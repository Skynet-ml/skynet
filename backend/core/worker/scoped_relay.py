"""Build the parent-side dispatcher for sandbox-scoped relay capabilities.

:class:`CallRecorder` writes each relay call into the run record (E28), with
every secret the call could carry removed by the store (E52 rule 3).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import threading
import time
from collections.abc import Collection, Generator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote, quote_plus, unquote, urlsplit

import httpx

from ..billing.budgets import BudgetError
from ..billing.env_connectors import CONNECTOR_ROUTES_KEY, escaped_forms, relay_request
from ..billing.guest_model_proxy import MCP_PATH, METHOD_HEADER
from ..billing.host_agent import HOST_AGENT_PATH, HOST_AGENT_TOKEN_KEY
from ..billing.mcp_broker import _pinned_address, _PinnedTransport
from ..billing.model_dispatch import ModelHTTPResult
from ..billing.model_gateway import MCP_RELAY_ROUTE_KEY, _tool_target
from ..billing.operation_pricing import UnpricedOperationError
from ..billing.usage_tags import LANE_KEY, current_tags
from ..config import settings
from ..models.blackbox import BLACKBOX_TARGET_REPO
from ..service_gateway.optimization.blackbox.repo_constraints import RoleEnvironment
from ..service_gateway.optimization.blackbox.repo_scorer import (
    _DECODED_HEADERS,
    _HEAD_END,
    _MCP_STREAMS,
    _SERVICE_BODY_BYTES,
    _SERVICE_TIMEOUT_SECONDS,
    SETUP_ALLOWANCE_SECONDS,
    _AnswerTooLargeError,
    _json_object,
    _streamed,
    _UnreachableError,
)
from ..service_gateway.optimization.blackbox.repo_services import RoleRelayError, mcp_endpoint
from ..storage.run_call_store import call_candidate_key

logger = logging.getLogger(__name__)

_MIN_DERIVED_FORM_CHARS = 8
# Run calls that serve no candidate, such as proposer calls; the calls route needs a nonempty id.
NO_CANDIDATE_ID = "run"
# The MCP streamable HTTP headers. Every other guest header, its relay token included, stays in the parent.
_MCP_REQUEST_HEADERS = frozenset({"accept", "content-type", "last-event-id", "mcp-protocol-version", "mcp-session-id"})
_MCP_ANSWER_HEADERS = frozenset({"mcp-session-id", "mcp-protocol-version"})
_MCP_METHODS = frozenset({"GET", "POST", "DELETE"})


class RunCallSink(Protocol):
    """Take one relay call for the run record; ``PostgresRunCallStore`` is the real one."""

    def record_call(self, optimization_id: str, call: dict[str, Any], secrets: list[str]) -> int:
        """Write one call with its secrets removed.

        Args:
            optimization_id: Owning job id.
            call: The call fields.
            secrets: Every secret value the call could carry.

        Returns:
            The call's sequence number.
        """
        ...


@dataclass(frozen=True)
class CallStart:
    """Mark when one relay call started, for its start time and its duration."""

    at: datetime
    clock: float

    def elapsed_ms(self) -> int:
        """Return the milliseconds since the call started.

        Returns:
            Whole milliseconds, never negative.
        """
        return max(0, round((time.monotonic() - self.clock) * 1000))


@dataclass(frozen=True)
class CallRecorder:
    """Write each relay call of one run, candidate and role into the run record (E28)."""

    sink: RunCallSink
    optimization_id: str
    candidate_id: str
    role: str

    @staticmethod
    def start() -> CallStart:
        """Mark the start of one call.

        Returns:
            The wall-clock start and a monotonic clock for its duration.
        """
        return CallStart(datetime.now(UTC), time.monotonic())

    def bind(self, *, candidate_id: str | None = None, role: str | None = None) -> CallRecorder:
        """Return a recorder for another candidate or role of the same run.

        Args:
            candidate_id: The candidate, or None to keep this one.
            role: The role, or None to keep this one.

        Returns:
            The new recorder.
        """
        return replace(
            self,
            candidate_id=self.candidate_id if candidate_id is None else candidate_id,
            role=self.role if role is None else role,
        )

    def record(
        self,
        started: CallStart,
        *,
        kind: str,
        target: str,
        status: str,
        request: bytes | str = b"",
        response: bytes | str = b"",
        failure_reason: str | None = None,
        cost_usd: float | None = None,
        secrets: Collection[str] = (),
        duration_ms: int | None = None,
    ) -> None:
        """Write one call; a store failure is logged and never fails the call itself.

        The write is synchronous: the caller waits for the row write and for
        the lock on the call's candidate row, which keeps ``seq`` in commit
        order. A recorder with no candidate id writes under the candidate of
        the parent's open usage scope, keyed by its Auto lane, else under
        ``NO_CANDIDATE_ID``.

        Args:
            started: When the call started.
            kind: ``model``, ``mcp``, ``stage``, ``evaluator`` or ``host``.
            target: The route, tool or host the call went to.
            status: ``ok``, ``failed`` or ``refused``, or the HTTP status code.
            request: The request body the box sent.
            response: The answer body.
            failure_reason: Why the call failed or was refused.
            cost_usd: What the call cost the run, when known.
            secrets: Every secret value the call could carry. The store
                removes their raw and JSON-escaped forms, and their base64,
                percent-encoded and Basic-auth forms from :func:`_secret_forms`.
            duration_ms: The duration another process measured, such as the
                egress proxy; None measures it from ``started``.
        """
        tags = current_tags()
        scoped = call_candidate_key(tags["candidate"], tags.get(LANE_KEY)) if tags.get("candidate") else ""
        call = {
            "candidate_id": self.candidate_id or scoped or NO_CANDIDATE_ID,
            "role": self.role,
            "kind": kind,
            "target": target,
            "started_at": started.at,
            "duration_ms": started.elapsed_ms() if duration_ms is None else duration_ms,
            "status": status,
            "failure_reason": failure_reason,
            "cost_usd": cost_usd,
            "request": _text(request),
            "response": _text(response),
        }
        try:
            forms = {form for secret in secrets if secret for form in _secret_forms(secret)}
            self.sink.record_call(self.optimization_id, call, sorted(forms))
        except Exception:
            # The record must never break the run; the exception text could hold a body, so it is not logged.
            logger.warning("Could not record a %s call for run %s.", kind, self.optimization_id)


def _secret_forms(secret: str) -> set[str]:
    """List the forms of one secret that an echoing server can send back.

    Besides the value itself, a server may echo it base64-encoded (standard
    or URL-safe) or percent-encoded (as in a URL query or a form body), and a
    Basic-auth token may come back decoded as ``user:password`` or as the
    password alone. Padding is left off each base64 form, so the padded form
    is masked too. Each form is also listed JSON-escaped with ``\\/`` (PHP)
    and with ``\\u0026``-style HTML escapes (Go), because a base64 form holds
    ``/`` and a decoded password may hold ``&``, ``<`` or ``>``.

    Args:
        secret: One nonempty secret value.

    Returns:
        The value and its derived forms, each with its escaped variants; at
        most 40 strings.
    """
    raw = secret.encode()
    derived = {encode(raw).decode().rstrip("=") for encode in (base64.b64encode, base64.urlsafe_b64encode)}
    # A standard base64 key holds "+", "/" and "=", which a URL or form encoder changes.
    derived |= {quote(secret), quote(secret, safe=""), quote_plus(secret)}
    token = secret.partition(" ")[2] if secret[:6].lower() == "basic " else secret
    derived.add(token)
    try:
        decoded = base64.b64decode(token, validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        decoded = ""
    # Only a decoded "user:password" is a Basic credential; other base64 text is left alone.
    _user, separator, password = decoded.partition(":")
    credential: set[str] = set()
    if separator and password and decoded.isprintable():
        # A decoded Basic password is a real secret at any length, so it skips the length floor.
        credential = {decoded, password}
    # A short derived form, such as the base64 "MQ" of "1", would mask ordinary text.
    forms = {secret} | credential | {form for form in derived if len(form) >= _MIN_DERIVED_FORM_CHARS}
    return forms | {variant for form in forms for variant in escaped_forms(form)}


def _text(body: bytes | str) -> str:
    """Return a body as text for the record.

    Args:
        body: Raw bytes or text.

    Returns:
        The UTF-8 text, with any bad bytes replaced.
    """
    return body.decode("utf-8", "replace") if isinstance(body, bytes) else body


def _mcp_error(status: int, message: str) -> ModelHTTPResult:
    """Return a relay refusal as a JSON error.

    Args:
        status: The HTTP status.
        message: Why the relay refused; it names the fix.

    Returns:
        The error answer.
    """
    error = {"error": {"type": "mcp_relay", "code": "mcp_relay", "message": message}}
    return ModelHTTPResult(status, "application/json", json.dumps(error).encode())


class McpRelay:
    """Send a role box's MCP requests to the servers of the role's own MCP file (E49, E55).

    The box's ``.mcp.json`` names only relay urls. The parent holds each real
    url and header, fills the secrets with :func:`relay_request`, and removes
    every filled value from the answer. A server outside the role's file gets
    a 403 that names the file to change. The relay url builder is
    ``repo_services.box_mcp_config``, so any harness can write the same urls.
    """

    def __init__(
        self,
        label: str,
        role: RoleEnvironment,
        *,
        secrets: Mapping[str, str] | None = None,
        bindings: Mapping[str, str] | None = None,
        secret_hosts: Mapping[str, str] | None = None,
        allowed_hosts: Mapping[str, Collection[str]] | None = None,
        budget: Any = None,
    ) -> None:
        """Bind the relay to one role and the run's secrets.

        Args:
            label: The role's label, such as ``[proposer]``.
            role: The role, with the servers of its own MCP file.
            secrets: The run's secret values, filled in here in the parent.
            bindings: ``user`` or ``managed`` for each secret name.
            secret_hosts: The one host each secret may go to.
            allowed_hosts: Each user secret's ``allowed_hosts`` from the run.
            budget: The run budget, which meters a call with a Skynet key.
        """
        self._label = label
        self._role = role
        self._secrets = dict(secrets or {})
        self._bindings = dict(bindings or {})
        self._secret_hosts = dict(secret_hosts or {})
        self._allowed_hosts = dict(allowed_hosts or {})
        self._budget = budget
        self._streams_lock = threading.Lock()
        self._open_streams = 0

    def dispatch(
        self, name: str, method: str, body: bytes, headers: Mapping[str, str], recorder: CallRecorder | None
    ) -> ModelHTTPResult:
        """Send one MCP request to its declared server and return the masked answer.

        Args:
            name: The server, as the box's relay url names it.
            method: ``GET``, ``POST`` or ``DELETE``.
            body: The client's raw request body.
            headers: The guest's headers; only the MCP headers go out.
            recorder: Writes the call into the run record; ``None`` records nothing.

        Returns:
            The answer, with its MCP session header. A GET event stream comes
            back with ``stream`` set, and the caller must read or close it.

        Raises:
            BudgetError: When the budget stops a metered call.
            UnpricedOperationError: When a metered call has no price.
        """
        if recorder is not None:
            recorder = recorder.bind(role=self._label.strip("[]"))
        label = f"MCP server {name!r}"
        try:
            if method not in _MCP_METHODS:
                raise RoleRelayError(f"{label} takes only GET, POST and DELETE requests.")
            url, declared = mcp_endpoint(self._label, self._role, name)
        except RoleRelayError as error:
            if recorder is not None:
                recorder.record(recorder.start(), kind="mcp", target=name, status="refused", failure_reason=str(error))
            return _mcp_error(403, str(error))
        # Only a GET listener streams, and it takes its place before the request goes out.
        listener = method == "GET"
        if listener:
            with self._streams_lock:
                opened = self._open_streams < _MCP_STREAMS
                self._open_streams += opened
            if not opened:
                return _mcp_error(503, f"{label} has {_MCP_STREAMS} event streams open already.")
        try:
            response = self._request(url, declared, label, name, method, body, headers, recorder)
        except BaseException:
            if listener:
                self._release()
            raise
        if listener and response.stream is None:
            self._release()
        return response

    def _release(self) -> None:
        """Free one event stream place."""
        with self._streams_lock:
            self._open_streams -= 1

    def _request(
        self,
        url: str,
        declared: Mapping[str, str],
        label: str,
        name: str,
        method: str,
        body: bytes,
        headers: Mapping[str, str],
        recorder: CallRecorder | None,
    ) -> ModelHTTPResult:
        """Send the request through :func:`relay_request` to the declared url, pinned and with no redirect.

        Args:
            url: The server's declared url.
            declared: The declared headers, with ``${SECRET}`` placeholders.
            label: ``MCP server '<name>'``, for the errors.
            name: The server, for the record target.
            method: The HTTP method.
            body: The raw request body.
            headers: The guest's headers.
            recorder: Writes the call into the run record.

        Returns:
            The masked answer.

        Raises:
            BudgetError: When the budget stops a metered call.
            UnpricedOperationError: When a metered call has no price.
        """
        overrides = {key.lower() for key in declared}
        guest = [
            (key, value)
            for key, value in headers.items()
            if key.lower() in _MCP_REQUEST_HEADERS and key.lower() not in overrides
        ]
        status: list[int] = []

        def send(address: str, filled: dict[str, str], content: bytes) -> bytes | Generator[bytes, None, None]:
            """Send the filled request once and return its header lines and body together.

            Args:
                address: The declared url.
                filled: The declared headers with their secrets filled in.
                content: The guest's body.

            Returns:
                The answer's header lines, a blank line, then its body; for a
                GET event stream, an iterator that yields them as they arrive.

            Raises:
                _UnreachableError: When the host resolves to a private,
                    loopback or link-local address.
                _AnswerTooLargeError: When the body passes 4 MiB.
            """
            try:
                pinned = _pinned_address(url, False, label=f"{label} url")
            except ValueError as error:
                # Not a secret refusal: relay_request's ValueError means only that.
                raise _UnreachableError(str(error)) from error
            transport = _PinnedTransport(url, pinned)
            with contextlib.ExitStack() as stack:
                stack.callback(transport.close)
                client = stack.enter_context(
                    httpx.Client(
                        transport=transport, follow_redirects=False, trust_env=False, timeout=_SERVICE_TIMEOUT_SECONDS
                    )
                )
                response = stack.enter_context(
                    client.stream(method, address, headers=[*guest, *filled.items()], content=content)
                )
                head = (
                    b"".join(
                        key + b": " + value + b"\r\n"
                        for key, value in response.headers.raw
                        if key.decode("latin-1").lower() not in _DECODED_HEADERS
                    )
                    + _HEAD_END[2:]
                )
                status.append(response.status_code)
                if method == "GET" and response.headers.get("content-type", "").lower().startswith("text/event-stream"):
                    return _streamed(stack.pop_all(), head, response.iter_bytes())
                answer = bytearray()
                for chunk in response.iter_bytes():
                    answer.extend(chunk)
                    if len(answer) > _SERVICE_BODY_BYTES:
                        raise _AnswerTooLargeError
                return head + bytes(answer)

        try:
            answer = relay_request(
                url=url,
                headers=declared,
                body=body,
                user_secrets=self._secrets,
                bindings=self._bindings,
                secret_hosts=self._secret_hosts,
                allowed_hosts=self._allowed_hosts,
                send=send,
                runtime=self._budget,
                recorder=recorder,
                kind="mcp",
                target=_tool_target(name, url, _json_object(body)),
            )
        except _AnswerTooLargeError:
            return _mcp_error(502, f"{label} answered with more than 4 MiB.")
        except (BudgetError, UnpricedOperationError):
            # These are ValueErrors too, but a budget stop, not a secret refusal.
            raise
        except ValueError as error:
            # relay_request names the secret and the host, never a value.
            return _mcp_error(403, f"{label} was refused: {error}")
        except (_UnreachableError, httpx.HTTPError) as error:
            # Only the type: h11 puts an illegal header value, a filled secret included, in its message.
            logger.warning("MCP server %s request failed in the parent: %s", name, type(error).__name__)
            return _mcp_error(502, f"{label} could not be reached.")
        if isinstance(answer, bytes):
            head, _, content = (b"\r\n" + answer).partition(_HEAD_END)
            return _answer(status[0], head, content)
        # The masker may hold the head back with the first event; read on until the head is whole.
        buffered = b"\r\n"
        try:
            for chunk in answer:
                buffered += chunk
                if _HEAD_END in buffered:
                    break
        except _AnswerTooLargeError:
            return _mcp_error(502, f"{label} answered with more than 4 MiB.")
        except BaseException:
            answer.close()
            raise
        head, _, content = buffered.partition(_HEAD_END)
        return replace(_answer(status[0], head, content), stream=self._events(answer))

    def _events(self, answer: Generator[bytes, None, None]) -> Generator[bytes, None, None]:
        """Pass an event stream on, and free its place when it ends or the reader closes it.

        Args:
            answer: The masked stream after its head.

        Yields:
            Each masked chunk.
        """
        try:
            yield from answer
        except _AnswerTooLargeError:
            return
        finally:
            answer.close()
            self._release()


def _answer(status: int, head: bytes, content: bytes) -> ModelHTTPResult:
    """Build the box's answer from the masked header lines and body.

    Args:
        status: The server's HTTP status.
        head: The masked header lines.
        content: The masked body.

    Returns:
        The answer with its content type and MCP headers only.
    """
    lines = [line.decode("latin-1").split(": ", 1) for line in head.split(b"\r\n") if b": " in line]
    content_type = next((value for key, value in lines if key.lower() == "content-type"), "application/json")
    kept = tuple((key, value) for key, value in lines if key.lower() in _MCP_ANSWER_HEADERS)
    return ModelHTTPResult(status, content_type, content, headers=kept)


def _mcp_name(path: str) -> str | None:
    """Return the server name of an MCP relay path, or None when the path could leave the relay prefix.

    Args:
        path: A path under ``/v1/mcp/``.

    Returns:
        The decoded name, or None for an empty name, a dot segment, or a
        raw slash, query or fragment.
    """
    segment = path[len(MCP_PATH) :]
    name = unquote(segment)
    if not segment or any(mark in segment for mark in "/?#") or name in {".", ".."}:
        return None
    return name


def model_forwarder(payload: dict[str, Any]) -> Any:
    """Restrict the supervisor relay to explicitly issued parent capabilities.

    Args:
        payload: Request carrying opaque model, tool, and evaluator routes.

    Returns:
        A callback compatible with the parent-owned model mailbox protocol.
    """
    routes: dict[str, tuple[str, frozenset[str]]] = {}
    model_paths = frozenset(
        {
            "/v1/messages",
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/_budget/state",
        }
    )

    def register(route: Any, permitted: frozenset[str]) -> None:
        """Register a trusted loopback descriptor for its allowed paths.

        Args:
            route: Opaque route descriptor placed by the trusted parent.
            permitted: Exact HTTP paths authorized for its token.

        Raises:
            ValueError: When the descriptor does not point to the parent loopback gateway.
        """
        if not isinstance(route, dict):
            return
        origin = urlsplit(route["url"])
        if (
            origin.scheme != "http"
            or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
            or origin.username
            or origin.password
            or origin.path.rstrip("/") != "/v1"
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Protected model routes must use the parent's loopback gateway.")
        routes[route["token"]] = (f"{origin.scheme}://{origin.netloc}", permitted)

    configs = [payload.get(key) for key in ("model_config", "reflection_model_config", "task_model_config")]
    scorer = payload.get("scorer")
    if isinstance(scorer, dict):
        configs.append(scorer.get("model"))
    for key in ("generation_models", "reflection_models", "extra_reflection_model_configs"):
        configs.extend(payload.get(key) or [])
    for config in configs:
        if isinstance(config, dict) and isinstance(config.get("extra"), dict):
            route = config["extra"].get("_skynet_budget_route")
            register(route, model_paths)
            if isinstance(route, dict):
                register(route.get("embedding"), frozenset({"/v1/embeddings", "/v1/_budget/state"}))
                if route.get(HOST_AGENT_TOKEN_KEY):
                    # Only a host proposer's own token reaches the host agent, and only there.
                    register({**route, "token": route[HOST_AGENT_TOKEN_KEY]}, frozenset({HOST_AGENT_PATH}))
    register(payload.get("_skynet_target_route"), model_paths)
    register(payload.get("_skynet_tools_route"), frozenset({"/v1/_mcp", "/v1/_budget/state"}))
    connector_routes = payload.get(CONNECTOR_ROUTES_KEY)
    if isinstance(connector_routes, dict):
        for route in connector_routes.values():
            register(route, frozenset({"/v1/_mcp", "/v1/_budget/state"}))
    register(payload.get("_skynet_packages_route"), frozenset({"/v1/_packages"}))
    register(payload.get("_skynet_evaluator_route"), frozenset({"/v1/_evaluator", "/v1/_budget/state"}))
    mcp_route = payload.get(MCP_RELAY_ROUTE_KEY)
    register(mcp_route, frozenset())
    # Each server of the role's MCP file has its own path under the prefix.
    mcp_tokens = {mcp_route["token"]} if isinstance(mcp_route, dict) else set()
    target = payload.get("target")
    evaluator_timeout = 630.0
    if isinstance(target, dict) and target.get("kind") == BLACKBOX_TARGET_REPO:
        # A repository score also prepares the version and opens its stage boxes, so the
        # guest waits up to its setup allowance longer; the parent must not cut it off first.
        evaluator_timeout += SETUP_ALLOWANCE_SECONDS

    def dispatch(token: str, path: str, body: dict[str, Any], headers: dict[str, str]) -> ModelHTTPResult:
        """Forward one request through its exact scoped capability.

        Args:
            token: Opaque capability token issued by the parent.
            path: Requested relay path.
            body: JSON request body.
            headers: Protocol headers safe to forward.

        Returns:
            The upstream status, content type, and response bytes.

        Raises:
            ValueError: When the token or requested path is outside its scope.
        """
        if token in mcp_tokens and path.startswith(MCP_PATH):
            if _mcp_name(path) is None:
                raise ValueError("The guest requested a path outside the MCP relay.")
            return _forward_mcp(routes[token][0] + path, token, body, headers)
        if token not in routes or path not in routes[token][1]:
            raise ValueError("The guest requested an unknown metered model capability.")
        timeout = evaluator_timeout if path == "/v1/_evaluator" else 600
        if path == HOST_AGENT_PATH:
            # One host CLI turn may run for its whole configured limit.
            timeout = settings.host_agent_timeout_seconds + 30
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            response = client.request(
                "GET" if path.endswith("/_budget/state") else "POST",
                routes[token][0] + path,
                headers={**headers, "Authorization": f"Bearer {token}"},
                json=body,
            )
        return ModelHTTPResult(
            response.status_code,
            response.headers.get("Content-Type", "application/json"),
            response.content,
        )

    return dispatch


def _forward_mcp(url: str, token: str, body: bytes, headers: Mapping[str, str]) -> ModelHTTPResult:
    """Send one MCP relay request to the parent gateway with its own method, and stream an event stream back.

    Args:
        url: The gateway's MCP relay url for the server.
        token: The MCP relay token.
        body: The client's raw body.
        headers: The mailbox headers, with the method header.

    Returns:
        The gateway's answer; a GET event stream comes back with ``stream`` set.
    """
    method = headers.get(METHOD_HEADER, "POST")
    sent = {key: value for key, value in headers.items() if key.lower() != "authorization"}
    client = httpx.Client(trust_env=False, timeout=600)
    try:
        response = client.send(
            client.build_request(method, url, headers={**sent, "Authorization": f"Bearer {token}"}, content=body),
            stream=True,
        )
    except BaseException:
        client.close()
        raise
    answer = ModelHTTPResult(
        response.status_code,
        response.headers.get("Content-Type", "application/json"),
        b"",
        headers=tuple((key, value) for key, value in response.headers.items() if key.lower() in _MCP_ANSWER_HEADERS),
    )
    if method == "GET" and answer.content_type.lower().startswith("text/event-stream"):
        return replace(answer, stream=_gateway_events(client, response))
    try:
        return replace(answer, body=response.read())
    finally:
        response.close()
        client.close()


def _gateway_events(client: httpx.Client, response: httpx.Response) -> Generator[bytes, None, None]:
    """Pass the gateway's event stream on, and close its connection at the end.

    Args:
        client: The client that holds the connection.
        response: The open event stream.

    Yields:
        Each chunk as it arrives.
    """
    try:
        yield from response.iter_bytes()
    finally:
        response.close()
        client.close()
