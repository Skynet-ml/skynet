"""Build the parent-side dispatcher for sandbox-scoped relay capabilities.

:class:`CallRecorder` writes each relay call into the run record (E28), with
every secret the call could carry removed by the store (E52 rule 3).
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote, quote_plus, urlsplit

import httpx

from ..billing.env_connectors import CONNECTOR_ROUTES_KEY, escaped_forms
from ..billing.host_agent import HOST_AGENT_PATH, HOST_AGENT_TOKEN_KEY
from ..billing.model_dispatch import ModelHTTPResult
from ..billing.usage_tags import current_tags
from ..config import settings
from ..models.blackbox import BLACKBOX_TARGET_REPO
from ..service_gateway.optimization.blackbox.repo_scorer import SETUP_ALLOWANCE_SECONDS

logger = logging.getLogger(__name__)

_MIN_DERIVED_FORM_CHARS = 8
# Run calls that serve no candidate, such as proposer calls; the calls route needs a nonempty id.
NO_CANDIDATE_ID = "run"


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
        the parent's open usage scope, else under ``NO_CANDIDATE_ID``.

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
        call = {
            "candidate_id": self.candidate_id or current_tags().get("candidate") or NO_CANDIDATE_ID,
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
