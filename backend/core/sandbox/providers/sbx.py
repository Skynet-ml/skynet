"""The async Docker Sandboxes (sbx) Cloud provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), ``harbor/environments/sbx/cloud/``:
the sandbox create, poll and delete flow, the endpoint credentials, the
process and output protocol, the multipart file transfer and the network
policy bodies come from there. Changes:

- The provider implements the async ``SandboxEnvironment`` contract over
  ``httpx``. The factory refuses it in production (``production_allowed=False``).
- No call retries (SKY-406 D3, D10). An HTTP error maps to one sandbox error
  group, and the message carries only the safe error code and the request id,
  never the response body.
- The start and run phase timeouts bound the create and each command. A
  command that runs past its limit or whose ``exec`` is cancelled is killed.
- A stop during the create, or after a create whose outcome is unknown (a
  cancel, a timeout, a transport error, a 5xx or an invalid 2xx answer),
  polls the sandbox list by the unique display name for up to
  ``ORPHAN_WAIT`` seconds and deletes the sandbox it finds. Only the listed
  pre-action refusals, a failed connect and no free pooled connection are
  definite, so a stop after one looks for nothing. A sandbox that shows in the list only after
  ``ORPHAN_WAIT`` leaks until its sbx lifetime ends.
- A lifetime above the sbx limit of 12 hours is capped, with a warning.
- The spec sizes map to the first sbx shape that is large enough. The sbx
  shapes have no disk size, so ``disk_mb`` has no effect.
- ``stop_job_sandboxes`` stops the live sandboxes of this process that carry
  the job tag, because the sbx create body has no labels.
- Harbor's local ``sbx`` CLI target, the Docker PAT exchange, credential
  renewal, retries, archive transfer and compose support are left out.
- As in Harbor, the delete flow checks a not-found answer against the
  sandbox list, and a 409 or 412 delete answer reads the sandbox once more
  for its current state and ETag.
- A command whose create answer is lost to a timeout or a cancel has no
  process name, so it cannot be killed. It ends when the sandbox does.
- With a budget the box is protected: it reserves its whole lifetime before
  the create call and settles after the stop at the runtime it measured
  (``core.billing.sbx_usage``). sbx cannot extend a sandbox's timeout, so the
  funded lifetime never grows.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import contextlib
import json
import logging
import math
import os
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

from core.billing.sbx_usage import BILLING_GRACE_MS, SbxUsageReservation

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError, network_mode
from ..environment import JOB_TAG, ExecResult, NetworkPolicy, OutputSink, SandboxEnvironment
from ..errors import (
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from ..resources import ResourceMode, SandboxSpec
from ..timeouts import Phase, PhaseTimeouts

if TYPE_CHECKING:
    from core.billing.runtime import BudgetRuntime

try:
    from python_multipart.multipart import MultipartParser, parse_options_header
except ImportError:
    MultipartParser = None
    parse_options_header = None

logger = logging.getLogger(__name__)

TOKEN_ENV = "SBX_TOKEN"
TOKEN_FILE_ENV = "SBX_TOKEN_FILE"
MANAGEMENT_URL = "https://connect.docker.com/sandboxes"
POLICY_URL = "https://api.sandboxes-cloud.docker.com"
DEFAULT_IMAGE = "docker/sandbox-templates:shell-docker"
STOP_TIMEOUT = 60.0
ORPHAN_WAIT = 30.0
# sbx can stay in "deleting" past STOP_TIMEOUT after it accepts a DELETE, so
# the stop waits this much more before it charges the whole lifetime.
# STOP_TIMEOUT plus DELETE_WAIT must fit in the half of the billing grace
# that is left for the delete; a test keeps them in step.
DELETE_WAIT = 240.0
KILL_TIMEOUT = 30.0
POLL_INTERVAL = 2.0
RPC_TIMEOUT = 30.0
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
# The Docker provider has no download limit; this matches the Kernel provider's.
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
_MAX_LIFETIME_SECONDS = 43_200
# A protected box must run within half the billing grace; the other half
# covers the delete (STOP_TIMEOUT plus DELETE_WAIT).
_PROTECTED_START_LIMIT = BILLING_GRACE_MS / 2000
_MAX_LIST_PAGES = 1000
_CONTROL_BYTES = 64 * 1024
_FILE_MODE = 0o644
_INT32 = (-(2**31), 2**31 - 1)
_UINT64_MAX = 2**64 - 1
_STARTING_STATUSES = frozenset({"creating", "starting"})
_CLIENT_ERRORS = frozenset(range(400, 500))
# Create answers that prove sbx made no sandbox: each refuses the request
# before any action (402 no credit, 405 wrong method for the route, 413 and
# 415 a body sbx does not take). 400, 401, 403, 404, 422 and 429 refuse a bad,
# unauthorized, unrouted, invalid or rate-limited request. A 409 cannot be a
# replay of an earlier create: the create goes out once, with a fresh
# idempotency key and a unique display name. Any other 4xx (408, 499 and the
# rest) can come after the server acted, so the create stays unknown.
_CREATE_REFUSALS = frozenset({400, 401, 402, 403, 404, 405, 409, 413, 415, 422, 429})
_PRECONDITION_FAILED = frozenset({409, 412})
_SAFE_CODE = re.compile(r"[A-Za-z_]+")
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# The provider contract reports a timeout as exit code 124, like GNU timeout.
_TIMEOUT_EXIT_CODE = 124
_CREDENTIAL_PERMISSIONS = ["sandboxesExec", "sandboxesFilesRead", "sandboxesFilesWrite"]
_DOWNLOAD_PARTS = frozenset({b"metadata", b"content", b"error", b"stream-error"})
# The sbx Cloud shapes as (vCPUs, memory in MiB).
_SHAPES = ((1, 2048), (2, 4096), (4, 8192), (8, 16384), (16, 32768))
# Every live sandbox of this process, for ``stop_job_sandboxes``.
_LIVE: set[SbxEnvironment] = set()

SBX_CAPABILITIES = EnvironmentCapabilities(
    name="sbx",
    billable=True,
    network_modes=frozenset(NetworkMode),
    dynamic_network_policy=True,
    max_vcpus=16,
)


class _MissingFileError(Exception):
    """Signal a download part that reports a missing file."""


def _make_transport() -> httpx.AsyncBaseTransport | None:
    """Return the HTTP transport of every client; tests swap in a fake.

    Returns:
        ``None``, so ``httpx`` uses its default network transport.
    """
    return None


def _check_token(token: str) -> str:
    """Reject an empty bearer token or one with control or space characters.

    Args:
        token: The stripped token.

    Returns:
        The token.

    Raises:
        SandboxFatalError: The token cannot go into an Authorization header.
    """
    if not token or any(ord(char) <= 32 or ord(char) == 127 for char in token):
        raise SandboxFatalError(f"Set {TOKEN_ENV} or {TOKEN_FILE_ENV} to a valid bearer token.")
    return token


def _management_token() -> str:
    """Read the management token from ``SBX_TOKEN_FILE`` or ``SBX_TOKEN``.

    Returns:
        The checked token.

    Raises:
        SandboxFatalError: The token file cannot be read, or no valid token is set.
    """
    path = os.environ.get(TOKEN_FILE_ENV)
    if path:
        try:
            token = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise SandboxFatalError(f"The {TOKEN_FILE_ENV} file cannot be read: {type(error).__name__}.") from None
    else:
        token = os.environ.get(TOKEN_ENV, "").strip()
    return _check_token(token)


def _client(base_url: str, token: str) -> httpx.AsyncClient:
    """Make an HTTP client that sends one bearer token.

    Args:
        base_url: The base URL of relative request paths.
        token: The checked bearer token.

    Returns:
        The client. It does not follow redirects, so the token stays on this host.
    """
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=RPC_TIMEOUT,
        transport=_make_transport(),
    )


def _error_code(response: httpx.Response) -> str:
    """Return the safe error code of an error answer.

    Args:
        response: The error answer.

    Returns:
        The JSON ``code`` when it has only letters and underscores, else ``"unknown"``.
    """
    try:
        payload = response.json()
    except ValueError:
        return "unknown"
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else "unknown"


def _status_error(response: httpx.Response, action: str) -> Exception:
    """Map an error answer to the sandbox error of its group.

    Args:
        response: The non-2xx answer.
        action: What the provider was doing, for the message.

    Returns:
        ``SandboxTransientError`` for 429 and 5xx, ``SandboxFatalError`` for
        401 and 403, ``SandboxRejectedError`` for 400, 409 and 422, and
        ``SandboxInfraError`` for any other status.
    """
    status = response.status_code
    request_id = response.headers.get("x-request-id", "")
    # The body can echo command arguments or credentials, so only the code goes out.
    detail = f"HTTP {status}, {_error_code(response)}"
    if _SAFE_REQUEST_ID.fullmatch(request_id):
        detail += f", request {request_id}"
    message = f"sbx could not {action} ({detail})."
    if status == 429 or status >= 500:
        return SandboxTransientError(message)
    if status in (401, 403):
        return SandboxFatalError(message)
    if status in (400, 409, 422):
        return SandboxRejectedError(message)
    return SandboxInfraError(message)


def _not_found(response: httpx.Response) -> bool:
    """Tell whether an answer says that the resource does not exist.

    Args:
        response: The answer.

    Returns:
        True for a 404 with the ``notFound`` code.
    """
    return response.status_code == 404 and _error_code(response) == "notFound"


async def _call(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    action: str,
    allow_not_found: bool = False,
    accept: frozenset[int] = frozenset(),
    **kwargs: Any,
) -> httpx.Response | None:
    """Send one request with no retry and check its status.

    Args:
        client: The HTTP client.
        method: The HTTP method.
        url: The path, or a full URL.
        action: What the request does, for error messages.
        allow_not_found: Return ``None`` for a ``notFound`` 404 instead of raising.
        accept: Error statuses to return to the caller instead of raising.
        **kwargs: More ``httpx`` request arguments.

    Returns:
        The 2xx or accepted answer, or ``None`` for an allowed not-found answer.

    Raises:
        SandboxTransientError: The connection failed or timed out.
        SandboxInfraError: ``httpx`` could not read the answer, for example
            a body that does not decode.
        SandboxError: The answer is not 2xx; see ``_status_error``.
    """
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TransportError as error:
        raise SandboxTransientError(f"sbx could not {action}: {type(error).__name__}.") from None
    except httpx.HTTPError as error:
        raise SandboxInfraError(f"sbx could not {action}: {type(error).__name__}.") from None
    if response.is_success or response.status_code in accept:
        return response
    if allow_not_found and _not_found(response):
        return None
    raise _status_error(response, action)


def _json(response: httpx.Response, action: str) -> dict[str, Any]:
    """Decode an answer that must be a JSON object.

    Args:
        response: The 2xx answer.
        action: What the request did, for the error message.

    Returns:
        The decoded object.

    Raises:
        SandboxInfraError: The body is not a JSON object.
    """
    try:
        value = response.json()
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise SandboxInfraError(f"sbx gave an invalid answer to {action}.")
    return value


def _resource_path(name: object, collection: str) -> str:
    """Turn a resource name such as ``sandboxes/<id>`` into its API path.

    Args:
        name: The ``name`` field of the resource.
        collection: The expected collection, ``sandboxes`` or ``processes``.

    Returns:
        ``/v1/<collection>/<quoted id>``.

    Raises:
        SandboxInfraError: The name is not one plain id in this collection.
    """
    prefix = f"{collection}/"
    if not isinstance(name, str) or not name.startswith(prefix):
        raise SandboxInfraError(f"sbx gave an invalid {collection} name.")
    resource_id = name[len(prefix) :]
    if not resource_id or "/" in resource_id or resource_id in {".", ".."}:
        raise SandboxInfraError(f"sbx gave an invalid {collection} name.")
    return f"/v1/{collection}/{quote(resource_id, safe='')}"


def _core(sandbox: dict[str, Any]) -> dict[str, Any]:
    """Return the ``core`` object of a sandbox resource.

    Args:
        sandbox: The sandbox resource.

    Returns:
        The ``core`` object, or an empty one.
    """
    core = sandbox.get("core")
    return core if isinstance(core, dict) else {}


def _status(sandbox: dict[str, Any]) -> str:
    """Return the safe lifecycle status of a sandbox resource.

    Args:
        sandbox: The sandbox resource.

    Returns:
        The status when it has only letters and underscores, else ``"unknown"``.
    """
    status = _core(sandbox).get("status")
    return status if isinstance(status, str) and _SAFE_CODE.fullmatch(status) else "unknown"


def _uint64(value: object, field: str) -> int:
    """Read a ProtoJSON uint64, which may come as a number or a decimal string.

    Args:
        value: The field value.
        field: The field name, for the error message.

    Returns:
        The integer.

    Raises:
        SandboxInfraError: The value is not a uint64.
    """
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _UINT64_MAX:
        raise SandboxInfraError(f"sbx gave an invalid {field}.")
    return value


def _shape(spec: SandboxSpec) -> tuple[int, int]:
    """Pick the smallest sbx shape that holds the requested sizes.

    Args:
        spec: The sandbox to open. ``validate_spec`` already checked it.

    Returns:
        The vCPUs and the memory in MiB.

    Raises:
        SandboxSpecError: No sbx shape holds the requested memory.
    """
    request = spec.resources
    if request.mode is ResourceMode.IGNORE:
        return _SHAPES[0]
    memory = request.memory_mb or 0
    for cpus, memory_mib in _SHAPES:
        if cpus >= request.cpus and memory_mib >= memory:
            return cpus, memory_mib
    raise SandboxSpecError(f"No sbx sandbox shape has {request.cpus} vCPUs and {memory} MB of memory.")


def _policy_body(policy: NetworkPolicy) -> dict[str, Any]:
    """Build the sbx network policy body for one egress policy.

    Args:
        policy: The egress policy.

    Returns:
        The JSON body of the network policy PUT.
    """
    mode = network_mode(disabled=policy.disabled, allowed_hosts=tuple(policy.allowed_hosts))
    if mode is NetworkMode.OPEN:
        return {"mode": "allow-all"}
    if mode is NetworkMode.DENY_ALL:
        return {"mode": "deny-all"}
    return {"mode": "deny-all", "allowNetworks": list(dict.fromkeys(policy.allowed_hosts))}


def _upload_body(path: str, data: bytes, boundary: str) -> bytes:
    """Build the multipart body of one file upload.

    Args:
        path: The absolute target path.
        data: The file bytes.
        boundary: The multipart boundary.

    Returns:
        The ``multipart/form-data`` body with a metadata and a content part.
    """
    metadata = json.dumps({"path": path, "mode": _FILE_MODE}, separators=(",", ":"))
    return (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
            f"Content-Type: application/json\r\n\r\n{metadata}"
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="content"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )


async def _uncancellable(future: asyncio.Future[Any]) -> Any:
    """Wait for a worker-thread call that a cancel cannot stop.

    A thread keeps running after a cancel, so on a cancel this waits for it to
    end before it raises the cancel again. The caller then never races it.

    Args:
        future: The ``asyncio.to_thread`` call, wrapped in a future.

    Returns:
        The result of the call.
    """
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            # The call's own error must not replace the cancel being raised.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(future)
        raise


async def _bounded(awaitable: Awaitable[Any], action: str) -> bool:
    """Wait for a cleanup call for at most ``STOP_TIMEOUT`` seconds.

    Args:
        awaitable: The cleanup call.
        action: What the call does, for the log.

    Returns:
        True when the call finished without an error.
    """
    try:
        await asyncio.wait_for(awaitable, STOP_TIMEOUT)
    except Exception as error:
        logger.warning("sbx could not %s: %s", action, type(error).__name__)
        return False
    return True


def _is_sandbox(sandbox: dict[str, Any], path: str, uid: str | None) -> bool:
    """Tell whether a listed sandbox is the one at this path or with this uid.

    Args:
        sandbox: One item of the sandbox list.
        path: The API path of the sandbox.
        uid: The uid of the sandbox, when known.

    Returns:
        True when the name maps to the path or the uid matches.
    """
    if uid is not None and sandbox.get("uid") == uid:
        return True
    try:
        return _resource_path(sandbox.get("name"), "sandboxes") == path
    except SandboxInfraError:
        return False


async def _find(client: httpx.AsyncClient, match: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    """Look through every page of the sandbox list for one sandbox.

    Args:
        client: The management client.
        match: Tells whether a listed sandbox is the one to find.

    Returns:
        The first listed sandbox that matches, or ``None``.

    Raises:
        SandboxInfraError: The sandbox list is invalid.
        SandboxError: A request failed; see ``_call``.
    """
    token = ""
    seen: set[str] = set()
    for _ in range(_MAX_LIST_PAGES):
        response = await _call(
            client,
            "GET",
            "/v1/sandboxes",
            action="list the sandboxes",
            params={"pageToken": token} if token else None,
        )
        page = _json(response, "list the sandboxes")
        sandboxes = page.get("sandboxes", [])
        if not isinstance(sandboxes, list):
            raise SandboxInfraError("sbx gave an invalid sandbox list.")
        for sandbox in sandboxes:
            if isinstance(sandbox, dict) and match(sandbox):
                return sandbox
        token = page.get("nextPageToken", "")
        if not isinstance(token, str) or token in seen:
            raise SandboxInfraError("sbx gave an invalid sandbox list.")
        if not token:
            return None
        seen.add(token)
    raise SandboxInfraError("sbx gave an invalid sandbox list.")


async def _read(client: httpx.AsyncClient, path: str, uid: str | None) -> dict[str, Any] | None:
    """Read one sandbox, and check a not-found answer against the list.

    Args:
        client: The management client.
        path: The API path of the sandbox.
        uid: The uid of the sandbox, when known.

    Returns:
        The sandbox resource, its list entry when only the list still has it,
        or ``None`` when neither has it.

    Raises:
        SandboxError: A request failed; see ``_call``.
    """
    response = await _call(client, "GET", path, action="read the sandbox", allow_not_found=True)
    if response is not None:
        return _json(response, "read the sandbox")
    # As in Harbor, a not-found read can be a routing miss, so the sandbox is
    # gone only when the list does not show it either.
    return await _find(client, lambda sandbox: _is_sandbox(sandbox, path, uid))


@dataclass
class _DeleteState:
    """What a stop knows about its delete of one sandbox.

    ``sent`` is true while a DELETE is in flight or after sbx accepted it; an
    answer that is not 2xx clears it. ``accepted`` is true after sbx answered
    a DELETE with 2xx. ``on_its_way`` is true after sbx accepted a DELETE or
    showed the sandbox as deleting. Only then does a missing sandbox count as
    gone; before, a miss can be list lag.
    """

    sent: bool = False
    accepted: bool = False
    on_its_way: bool = False


def _lag_over(missing_since: float | None) -> tuple[float, bool]:
    """Track how long a sandbox is missing before any proof that it is gone.

    Args:
        missing_since: When the sandbox first went missing, or ``None``.

    Returns:
        The first-miss time, and whether the miss lasted ``ORPHAN_WAIT``.
    """
    now = asyncio.get_running_loop().time()
    if missing_since is None:
        return now, False
    return missing_since, now - missing_since >= ORPHAN_WAIT


async def _delete(
    client: httpx.AsyncClient, path: str, uid: str | None = None, state: _DeleteState | None = None
) -> None:
    """Delete one sandbox with its current ETag and wait until it is gone.

    Args:
        client: The management client.
        path: The API path of the sandbox.
        uid: The uid of the sandbox, when known, to find it in the list.
        state: Updated as the delete goes on, so a caller whose wait for the
            delete ends early knows whether sbx may be deleting the sandbox.

    Raises:
        SandboxInfraError: The sandbox has no ETag, or never showed in
            ``ORPHAN_WAIT`` seconds.
        SandboxError: A request failed; see ``_call``.
    """
    state = state if state is not None else _DeleteState()
    refreshed = False
    missing_since: float | None = None
    while True:
        sandbox = await _read(client, path, uid)
        if sandbox is None:
            if state.on_its_way:
                return
            missing_since, over = _lag_over(missing_since)
            if over:
                raise SandboxInfraError("sbx never showed the sandbox to delete.")
            await asyncio.sleep(POLL_INTERVAL)
            continue
        status = _status(sandbox)
        if status == "deleting":
            state.on_its_way = True
        elif not state.accepted:
            # After a 2xx DELETE, sbx can still show the old status for a
            # while, so the loop only polls until the sandbox goes.
            etag = _core(sandbox).get("etag")
            if not isinstance(etag, str) or not etag:
                raise SandboxInfraError("sbx gave no ETag for the sandbox delete.")
            state.sent = True
            try:
                response = await _call(
                    client,
                    "DELETE",
                    path,
                    action="delete the sandbox",
                    allow_not_found=True,
                    accept=_PRECONDITION_FAILED,
                    headers={"If-Match": etag, "Idempotency-Key": str(uuid.uuid4())},
                )
            except Exception:
                # No 2xx answer: nothing shows that sbx took the DELETE. A
                # cancel is not caught, so a DELETE cut off in flight stays sent.
                state.sent = False
                raise
            if response is None or response.status_code in _PRECONDITION_FAILED:
                # sbx did not accept this DELETE, so it proves nothing about
                # the sandbox: a 404 can be a routing miss, as for a read.
                state.sent = False
                if response is not None:
                    if refreshed:
                        raise _status_error(response, "delete the sandbox")
                    # The ETag changed or another delete runs: read the sandbox
                    # once more for its state and ETag. This refreshes the
                    # precondition; it does not retry a failed call blindly.
                    refreshed = True
                    continue
            else:
                state.accepted = state.on_its_way = True
        await asyncio.sleep(POLL_INTERVAL)


async def _wait_gone(client: httpx.AsyncClient, path: str, uid: str | None, state: _DeleteState) -> bool:
    """Wait up to ``DELETE_WAIT`` seconds for a deleting sandbox to go away.

    Args:
        client: The management client.
        path: The API path of the sandbox.
        uid: The uid of the sandbox, when known, to find it in the list.
        state: What the stop knows about its delete.

    Returns:
        True when the sandbox is gone after sbx accepted the DELETE or showed
        it as deleting. False when it is not deleting and sbx accepted no
        DELETE, stays missing for ``ORPHAN_WAIT`` with no such proof, a read
        fails or the wait ends.
    """
    missing_since: float | None = None
    try:
        async with asyncio.timeout(DELETE_WAIT):
            while True:
                sandbox = await _read(client, path, uid)
                if sandbox is None:
                    if state.on_its_way:
                        return True
                    missing_since, over = _lag_over(missing_since)
                    if over:
                        return False
                elif _status(sandbox) == "deleting":
                    state.on_its_way = True
                elif not state.accepted:
                    return False
                await asyncio.sleep(POLL_INTERVAL)
    except Exception as error:
        logger.warning("sbx could not see the deleting sandbox go away: %s", type(error).__name__)
        return False


async def _kill(client: httpx.AsyncClient, process_path: str) -> None:
    """Kill a running command and log, not raise, a failure.

    Args:
        client: The runtime client.
        process_path: The API path of the process.
    """
    try:
        await asyncio.wait_for(
            _call(
                client,
                "POST",
                f"{process_path}/signal",
                action="kill the command",
                allow_not_found=True,
                json={"signal": "kill"},
            ),
            KILL_TIMEOUT,
        )
    except Exception as error:
        logger.warning("sbx could not kill the command: %s", type(error).__name__)


class _Capture:
    """Collect the output of one process and stream it as UTF-8 text."""

    def __init__(self, on_output: OutputSink | None) -> None:
        """Start with no output.

        Args:
            on_output: Receives ``(stream, text)`` pairs, or ``None``.
        """
        self.on_output = on_output
        self.sequence = 0
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.decoders = {name: codecs.getincrementaldecoder("utf-8")("replace") for name in self.buffers}

    def accept(self, chunk: object) -> None:
        """Add one output chunk, skipping a chunk that came before.

        Args:
            chunk: One item of the ``chunks`` list.

        Raises:
            SandboxInfraError: The chunk is malformed, or the output ran past
                ``MAX_OUTPUT_BYTES``.
        """
        if not isinstance(chunk, dict):
            raise SandboxInfraError("sbx gave an invalid output chunk.")
        sequence = _uint64(chunk.get("streamSequence"), "streamSequence")
        if sequence == 0:
            raise SandboxInfraError("sbx gave an invalid streamSequence.")
        if sequence <= self.sequence:
            return
        stream = chunk.get("stream")
        if stream not in self.buffers:
            raise SandboxInfraError("sbx gave an output chunk with an unknown stream.")
        encoded = chunk.get("data", "")
        if not isinstance(encoded, str):
            raise SandboxInfraError("sbx gave output data that is not base64.")
        # ProtoJSON allows padded and unpadded, standard and URL-safe base64.
        encoded = encoded.replace("-", "+").replace("_", "/")
        try:
            data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        except (binascii.Error, ValueError):
            raise SandboxInfraError("sbx gave output data that is not base64.") from None
        if sum(len(buffer) for buffer in self.buffers.values()) + len(data) > MAX_OUTPUT_BYTES:
            raise SandboxInfraError(f"The command wrote more than {MAX_OUTPUT_BYTES} bytes of output.")
        self.buffers[stream].extend(data)
        self.sequence = sequence
        self._emit(stream, data, final=False)

    def flush(self) -> None:
        """Send the text that the decoders still hold."""
        for stream in self.buffers:
            self._emit(stream, b"", final=True)

    def _emit(self, stream: str, data: bytes, *, final: bool) -> None:
        """Decode bytes and send the complete characters to the sink.

        Args:
            stream: ``stdout`` or ``stderr``.
            data: The new bytes.
            final: True to flush a partial character as a replacement.
        """
        if self.on_output is None:
            return
        text = self.decoders[stream].decode(data, final=final)
        if text:
            self.on_output(stream, text)

    def result(self, exit_code: int, *, timed_out: bool = False) -> ExecResult:
        """Build the command result.

        Args:
            exit_code: The process exit code.
            timed_out: The command ran past its time limit.

        Returns:
            The exit code and the decoded output.
        """
        return ExecResult(
            exit_code=exit_code,
            stdout=self.buffers["stdout"].decode("utf-8", "replace"),
            stderr=self.buffers["stderr"].decode("utf-8", "replace"),
            timed_out=timed_out,
        )


async def _read_download(response: httpx.Response, path: str) -> str | None:
    """Check and decode one streamed multipart download answer.

    Args:
        response: The streamed answer, not read yet.
        path: The path that the download asked for.

    Returns:
        The file text, or ``None`` when the file does not exist.

    Raises:
        SandboxInfraError: The answer is invalid, incomplete or above
            ``MAX_DOWNLOAD_BYTES``.
        SandboxError: The answer is not 2xx; see ``_status_error``.
    """
    if not response.is_success:
        await response.aread()
        if _not_found(response):
            return None
        raise _status_error(response, "read a file")
    media_type, options = parse_options_header(response.headers.get("content-type"))
    boundary = options.get(b"boundary", b"")
    if media_type != b"multipart/mixed" or not 1 <= len(boundary) <= 70 or any(not 32 <= b <= 126 for b in boundary):
        raise SandboxInfraError("sbx gave a download with no valid multipart boundary.")
    decoder = _Download(path)
    parser = MultipartParser(boundary, decoder.callbacks())
    received = 0
    try:
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > MAX_DOWNLOAD_BYTES:
                raise SandboxInfraError(f"The sbx download of {path!r} is above the {MAX_DOWNLOAD_BYTES} byte limit.")
            parser.write(chunk)
        parser.finalize()
    except _MissingFileError:
        return None
    except ValueError:
        raise SandboxInfraError("sbx gave a malformed multipart download.") from None
    # finalize() accepts a cut stream, so the closing boundary must have arrived.
    if not decoder.ended or decoder.phase != "complete":
        raise SandboxInfraError("The sbx download ended before the whole file arrived.")
    return bytes(decoder.content).decode("utf-8")


class _Download:
    """Check the one-file multipart download and collect the file bytes."""

    def __init__(self, source: str) -> None:
        """Start before the first part.

        Args:
            source: The path that the download asked for.
        """
        self.source = source
        self.phase = "initial"
        self.ended = False
        self.part = b""
        self.header_name = bytearray()
        self.header_value = bytearray()
        self.headers: dict[bytes, bytes] = {}
        self.control = bytearray()
        self.content = bytearray()

    def callbacks(self) -> dict[str, Any]:
        """Return the parser callbacks.

        Returns:
            The callback map of ``MultipartParser``.
        """
        return {
            "on_part_begin": self.part_begin,
            "on_header_field": self.header_field,
            "on_header_value": self.header_value_data,
            "on_header_end": self.header_end,
            "on_headers_finished": self.headers_finished,
            "on_part_data": self.part_data,
            "on_part_end": self.part_end,
            "on_end": self.end,
        }

    def part_begin(self) -> None:
        """Reset the state of one part."""
        self.headers.clear()
        self.control.clear()

    def header_field(self, data: bytes, start: int, end: int) -> None:
        """Collect a piece of a header name.

        Args:
            data: The parser buffer.
            start: The start of the piece.
            end: The end of the piece.
        """
        self.header_name.extend(data[start:end])

    def header_value_data(self, data: bytes, start: int, end: int) -> None:
        """Collect a piece of a header value.

        Args:
            data: The parser buffer.
            start: The start of the piece.
            end: The end of the piece.
        """
        self.header_value.extend(data[start:end])

    def header_end(self) -> None:
        """Keep one complete header.

        Raises:
            SandboxInfraError: The part repeats a header.
        """
        name = bytes(self.header_name).lower()
        if name in self.headers:
            raise SandboxInfraError("sbx gave a download part with a repeated header.")
        self.headers[name] = bytes(self.header_value)
        self.header_name.clear()
        self.header_value.clear()

    def headers_finished(self) -> None:
        """Check the part name, its media type and the part order.

        Raises:
            SandboxInfraError: The part is unexpected or out of order.
        """
        _, disposition = parse_options_header(self.headers.get(b"content-disposition"))
        self.part = disposition.get(b"name", b"")
        if self.part not in _DOWNLOAD_PARTS:
            raise SandboxInfraError("sbx gave an unexpected download part.")
        media_type, _ = parse_options_header(self.headers.get(b"content-type"))
        expected = b"application/octet-stream" if self.part == b"content" else b"application/json"
        if media_type != expected:
            raise SandboxInfraError("sbx gave a download part with an unexpected media type.")
        if self.part == b"metadata" and self.phase != "initial":
            raise SandboxInfraError("sbx gave more than one file or parts out of order.")
        if self.part == b"content" and self.phase != "metadata":
            raise SandboxInfraError("sbx gave file content before its metadata.")

    def part_data(self, data: bytes, start: int, end: int) -> None:
        """Collect part bytes.

        Args:
            data: The parser buffer.
            start: The start of the piece.
            end: The end of the piece.

        Raises:
            SandboxInfraError: A control part is too large.
        """
        if self.part == b"content":
            self.content.extend(data[start:end])
            return
        self.control.extend(data[start:end])
        if len(self.control) > _CONTROL_BYTES:
            raise SandboxInfraError("sbx gave download metadata that is too large.")

    def part_end(self) -> None:
        """Check a finished metadata or error part.

        Raises:
            _MissingFileError: The error part reports a missing file.
            SandboxInfraError: The part is invalid or reports another error.
        """
        if self.part == b"content":
            self.phase = "complete"
            return
        try:
            value = json.loads(self.control.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise SandboxInfraError("sbx gave download metadata that is not JSON.") from None
        if not isinstance(value, dict):
            raise SandboxInfraError("sbx gave download metadata that is not an object.")
        if self.part in {b"error", b"stream-error"}:
            failure = value.get("failure") if self.part == b"error" else value
            code = failure.get("code") if isinstance(failure, dict) else None
            if code == "notFound":
                raise _MissingFileError
            safe = code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else "unknown"
            raise SandboxInfraError(f"sbx could not read the file ({safe}).")
        if value.get("path") != self.source:
            raise SandboxInfraError("sbx gave another file than the one asked for.")
        # An omitted protobuf scalar means zero, so a missing mode is a valid mode 000.
        mode = value.get("mode", 0)
        if type(mode) is not int or not 0 <= mode <= 0xFFFFFFFF:
            raise SandboxInfraError("sbx gave an invalid file mode.")
        self.phase = "metadata"

    def end(self) -> None:
        """Record the closing boundary."""
        self.ended = True


class SbxEnvironment(SandboxEnvironment):
    """Run commands in one Docker Sandboxes (sbx) Cloud sandbox."""

    capabilities = SBX_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        timeouts: PhaseTimeouts | None = None,
        budget: BudgetRuntime | None = None,
        usage_factory: Callable[..., Any] = SbxUsageReservation,
    ) -> None:
        """Check the spec and prepare the create call.

        Args:
            spec: The sandbox to open.
            timeouts: The phase timeouts; ``None`` sets no start or run limit.
            budget: The ``BudgetRuntime`` that funds the box, or ``None``.
            usage_factory: Makes the usage reservation of a protected box.

        Raises:
            SandboxSpecError: No sbx shape holds this spec, or a protected box
                has no operation key.
        """
        if budget is not None and spec.operation_key is None:
            raise SandboxSpecError("A protected sbx sandbox needs an operation key.")
        super().__init__(spec)
        self._budget = budget
        self._usage_factory = usage_factory
        self._usage: SbxUsageReservation | None = None
        # A stop that found the lost sandbox knows it exists, so the teardown
        # settles it ("leaked") instead of keeping the hold ("unknown"), also
        # when a cancel or an invalid name stops the delete.
        self._orphan_found = False
        # The path and uid of that sandbox, for the wait after a slow delete.
        self._orphan: tuple[str, str | None] | None = None
        self._start_limit = timeouts.seconds(Phase.START) if timeouts else None
        if budget is not None:
            # The hold assumes the sbx timeout clock starts within the billing
            # grace of the create call. A protected box must run within half
            # the grace, so the other half is left for the delete.
            limit = _PROTECTED_START_LIMIT
            self._start_limit = limit if self._start_limit is None else min(self._start_limit, limit)
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts else None
        tag = uuid.uuid4().hex
        self._create_key = tag
        cpus, memory_mib = _shape(spec)
        lifetime = math.ceil(spec.lifetime_seconds)
        if lifetime > _MAX_LIFETIME_SECONDS:
            logger.warning(
                "sbx caps the sandbox lifetime at %d seconds; the spec asks for %d.", _MAX_LIFETIME_SECONDS, lifetime
            )
        # The tag makes the name unique, so a stop can find a sandbox whose
        # create answer was lost.
        self._display_name = f"{spec.name or 'skynet'}-{tag[:12]}"
        self._usage_request = {
            "cpus": cpus,
            "memory_mib": memory_mib,
            "lifetime_ms": min(lifetime, _MAX_LIFETIME_SECONDS) * 1000,
        }
        self._create_body: dict[str, Any] = {
            "displayName": self._display_name,
            "imageRef": spec.image or DEFAULT_IMAGE,
            "resources": {"cpus": cpus, "memoryMib": memory_mib},
            "platform": {"os": "linux", "architecture": "amd64"},
            "features": {
                "timeouts": {
                    "timeout": f"{min(lifetime, _MAX_LIFETIME_SECONDS)}s",
                    "onTimeout": "delete",
                }
            },
        }
        self._management: httpx.AsyncClient | None = None
        self._runtime: httpx.AsyncClient | None = None
        self._path: str | None = None
        self._uid: str | None = None
        self._starting: asyncio.Future[None] | None = None
        self._create_dispatched = False
        self._started = False
        self._stop_task: asyncio.Future[None] | None = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the extra and a valid management token exist.

        Raises:
            SandboxFatalError: The ``sbx`` extra or a valid token is missing.
        """
        if MultipartParser is None:
            raise SandboxFatalError("The python-multipart package is not installed: install the `sbx` extra.")
        _management_token()

    async def start(self) -> None:
        """Create the sandbox, wait until it runs and get its runtime token.

        Any failure or cancel stops the sandbox before the error goes on.

        Raises:
            StartTimeoutError: The sandbox did not run within the start timeout.
            SandboxInfraError: The sandbox did not reach the running state, a
                stop ended the start, or the sandbox already started once.
            SandboxError: A request failed; see ``_call``.
        """
        if self._started:
            # The create idempotency key is fixed, so a second create would
            # get the first sandbox back.
            raise SandboxInfraError("The sbx sandbox already started once; it cannot start again.")
        self.preflight()
        self._started = True
        # A stop before the start leaves a finished stop task; drop it so the
        # stop after this start deletes the sandbox.
        self._stop_task = None
        self._management = _client(MANAGEMENT_URL, _management_token())
        _LIVE.add(self)
        self._starting = asyncio.ensure_future(self._open())
        try:
            await self._starting
        except BaseException as error:
            await self.stop()
            task = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and task is not None and not task.cancelling():
                raise SandboxInfraError("The sbx sandbox stopped during its start.") from None
            raise
        finally:
            self._starting = None

    async def _open(self) -> None:
        """Reserve the budget of a protected box, then create the sandbox.

        A cancel or ``stop()`` during the reservation waits for it to end,
        which can take up to the budget runtime's ``wait_timeout``. That wait
        is accepted: a reservation that ends unseen would leak its hold, and
        a ledger thread cannot be stopped from here.
        """
        if self._budget is not None:
            reserving = asyncio.ensure_future(
                asyncio.to_thread(
                    self._usage_factory, self._budget, self._usage_request, operation_key=self.spec.operation_key
                )
            )
            try:
                self._usage = await _uncancellable(reserving)
            except asyncio.CancelledError:
                # The reservation may finish after the cancel; keep it so the
                # teardown releases it instead of leaking it.
                if not reserving.cancelled() and reserving.exception() is None:
                    self._usage = reserving.result()
                raise
        await self._create()

    async def _create(self) -> None:
        """Create the sandbox, wait for it, get its token and set its policy.

        Raises:
            StartTimeoutError: The sandbox did not run within the start timeout.
            SandboxInfraError: The sandbox reached another state than running.
        """
        client = self._management
        limit = self._start_limit
        try:
            async with asyncio.timeout(limit) as deadline:
                self._create_dispatched = True
                if self._usage is not None:
                    self._usage.start_clock()
                try:
                    response = await _call(
                        client,
                        "POST",
                        "/v1/sandboxes",
                        action="create the sandbox",
                        accept=_CLIENT_ERRORS,
                        json=self._create_body,
                        headers={"Idempotency-Key": self._create_key},
                    )
                except SandboxTransientError as error:
                    # A failed connect, or no free pooled connection, proves
                    # the create never reached sbx, so the stop releases the
                    # hold. ``_call`` hides the httpx error as the cause but
                    # keeps it as the context.
                    if isinstance(error.__context__, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
                        self._create_dispatched = False
                    raise
                if not response.is_success:
                    # A refusal is definite: sbx made no sandbox, so a stop
                    # has no lost sandbox to look for.
                    if response.status_code in _CREATE_REFUSALS:
                        self._create_dispatched = False
                    raise _status_error(response, "create the sandbox")
                sandbox = _json(response, "create the sandbox")
                # Kept here, not in start(), so a stop that cancels the start
                # right after the create still deletes the sandbox.
                self._path = _resource_path(sandbox.get("name"), "sandboxes")
                uid = sandbox.get("uid")
                if not isinstance(uid, str) or not uid:
                    raise SandboxInfraError("sbx gave no sandbox uid.")
                self._uid = uid
                if self._usage is not None:
                    await _uncancellable(
                        asyncio.ensure_future(asyncio.to_thread(self._usage.confirm_created, uid, self._create_body))
                    )
                while _status(sandbox) in _STARTING_STATUSES:
                    await asyncio.sleep(POLL_INTERVAL)
                    response = await _call(client, "GET", self._path, action="read the sandbox")
                    sandbox = _json(response, "read the sandbox")
                status = _status(sandbox)
                if status != "running":
                    raise SandboxInfraError(f"The sbx sandbox did not start: {status}.")
                endpoint = _core(sandbox).get("endpoint")
                uri = endpoint.get("uri") if isinstance(endpoint, dict) else None
                if not isinstance(uri, str) or not uri.startswith("https://"):
                    raise SandboxInfraError("The running sbx sandbox has no endpoint.")
                response = await _call(
                    client,
                    "POST",
                    f"{self._path}/endpoint-credentials",
                    action="get the sandbox credentials",
                    json={"permissions": _CREDENTIAL_PERMISSIONS},
                )
                token = _json(response, "get the sandbox credentials").get("token")
                if not isinstance(token, str):
                    raise SandboxInfraError("sbx gave no sandbox token.")
                try:
                    token = _check_token(token.strip())
                except SandboxFatalError:
                    raise SandboxInfraError("sbx gave an invalid sandbox token.") from None
                self._runtime = _client(uri, token)
                policy = self.network_policy
                if policy is not None and (policy.disabled or policy.allowed_hosts):
                    await self._put_policy(policy)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise StartTimeoutError(limit) from None

    async def _put_policy(self, policy: NetworkPolicy) -> None:
        """Send one egress policy to the sbx network policy API.

        Args:
            policy: The checked policy.

        Raises:
            SandboxInfraError: The sandbox has no uid yet.
            SandboxError: The request failed; see ``_call``.
        """
        client, uid = self._management, self._uid
        if client is None or uid is None:
            raise SandboxInfraError("The sbx sandbox is not running.")
        await _call(
            client,
            "PUT",
            f"{POLICY_URL}/v1/sandboxes/{quote(uid, safe='')}/network-policy",
            action="set the network policy",
            json=_policy_body(policy),
        )

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Change the egress policy of the running sandbox.

        Args:
            policy: The checked policy.

        Raises:
            SandboxInfraError: The sandbox is not running.
        """
        if self._runtime is None:
            raise SandboxInfraError("The sbx sandbox is not running.")
        await self._put_policy(policy)

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> ExecResult:
        """Run one shell command in the sandbox and wait for it.

        Args:
            command: The shell command.
            cwd: The working directory; the sandbox default by default.
            env: Extra environment variables for this command, over the spec env.
            timeout_sec: The time limit; ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` pairs while the command runs.
            sudo: Run the command as root instead of the sandbox user.

        Returns:
            The exit code and the captured output. When the command runs past
            the time limit, it is killed first, and the result has exit code
            124, ``timed_out`` set and the output so far.

        Raises:
            SandboxInfraError: The sandbox is not running, or sbx gave an
                invalid process answer.
            SandboxRejectedError: An environment variable name is not valid.
        """
        runtime = self._runtime
        if runtime is None:
            raise SandboxInfraError("The sbx sandbox is not running.")
        merged = {**self.spec.env, **(env or {})}
        for key in merged:
            if not _ENV_KEY.fullmatch(key):
                raise SandboxRejectedError(f"{key!r} is not a valid environment variable name.")
        session = str(uuid.uuid4())
        body = {
            "cmd": ["bash", "-c", 'exec bash -c "$1" </dev/null', "skynet", command],
            "workingDir": cwd or "",
            "env": merged,
            "user": "root" if sudo else "",
            "session": session,
        }
        capture = _Capture(on_output)
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        process_path: str | None = None
        finished = False
        timed_out = False
        try:
            async with asyncio.timeout(limit) as deadline:
                response = await _call(
                    runtime,
                    "POST",
                    "/v1/processes",
                    action="start the command",
                    json=body,
                    headers={"Idempotency-Key": session},
                )
                process_path = _resource_path(_json(response, "start the command").get("name"), "processes")
                exit_code = await self._wait(runtime, process_path, capture)
            finished = True
        except TimeoutError:
            if not deadline.expired():
                raise
            timed_out = True
        finally:
            # A timeout, a cancel or a lost wait must not leave the command
            # running. With no process name yet there is nothing to kill.
            if not finished and process_path is not None:
                await asyncio.shield(_kill(runtime, process_path))
        if timed_out:
            return capture.result(_TIMEOUT_EXIT_CODE, timed_out=True)
        return capture.result(exit_code)

    @staticmethod
    async def _wait(runtime: httpx.AsyncClient, process_path: str, capture: _Capture) -> int:
        """Poll the process output until the process exits.

        Args:
            runtime: The runtime client.
            process_path: The API path of the process.
            capture: Collects the output.

        Returns:
            The exit code.

        Raises:
            SandboxInfraError: The output answer breaks the resume protocol.
        """
        loop = asyncio.get_running_loop()
        cursor = 0
        next_poll_at = 0.0
        while True:
            # Always yield, so the exec deadline can fire when a poll answers at once.
            await asyncio.sleep(max(next_poll_at - loop.time(), 0))
            next_poll_at = loop.time() + POLL_INTERVAL
            response = await _call(
                runtime,
                "GET",
                f"{process_path}/output",
                action="read the command output",
                params={"resumeFrom": str(cursor)},
            )
            output = _json(response, "read the command output")
            chunks = output.get("chunks", [])
            if not isinstance(chunks, list):
                raise SandboxInfraError("sbx gave an invalid output answer.")
            for chunk in chunks:
                capture.accept(chunk)
            next_cursor = _uint64(output.get("nextResumeFrom", 0), "nextResumeFrom")
            if next_cursor != capture.sequence or next_cursor < cursor:
                raise SandboxInfraError("The sbx output cursor does not match the output.")
            cursor = next_cursor
            if "exited" in output:
                exited = output["exited"]
                exit_code = exited.get("exitCode", 0) if isinstance(exited, dict) else None
                if type(exit_code) is not int or not _INT32[0] <= exit_code <= _INT32[1]:
                    raise SandboxInfraError("sbx gave an invalid exit code.")
                capture.flush()
                return exit_code

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the sandbox, making parent directories.

        Args:
            files: The text of each file, by absolute path.

        Raises:
            SandboxInfraError: The sandbox is not running, the parent
                directories could not be made, or sbx did not confirm a file.
        """
        runtime = self._runtime
        if runtime is None:
            raise SandboxInfraError("The sbx sandbox is not running.")
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {".", "/"})
        if parents:
            made = await self.exec("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents))
            if not made.ok:
                raise SandboxInfraError("sbx could not make the upload directories.")
        for path, text in files.items():
            boundary = uuid.uuid4().hex
            response = await _call(
                runtime,
                "POST",
                "/v1/files/upload",
                action="write a file",
                content=_upload_body(path, text.encode("utf-8"), boundary),
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            written = _json(response, "write a file").get("filesWritten")
            if type(written) is not int or written != 1:
                raise SandboxInfraError("sbx did not confirm exactly one written file.")

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the sandbox.

        Args:
            path: The absolute path of the file.

        Returns:
            The file text, or ``None`` when the file does not exist.

        Raises:
            SandboxTransientError: The connection failed or timed out.
            SandboxInfraError: The sandbox is not running, the download
                answer is invalid or incomplete, or it is above
                ``MAX_DOWNLOAD_BYTES``.
            SandboxError: The answer is not 2xx; see ``_status_error``.
        """
        runtime = self._runtime
        if runtime is None:
            raise SandboxInfraError("The sbx sandbox is not running.")
        # A streamed answer lets the size limit hold before the whole file is in memory.
        try:
            async with runtime.stream("GET", "/v1/files/download", params={"paths": path}) as response:
                return await _read_download(response, path)
        except httpx.TransportError as error:
            raise SandboxTransientError(f"sbx could not read a file: {type(error).__name__}.") from None
        except httpx.HTTPError as error:
            raise SandboxInfraError(f"sbx could not read a file: {type(error).__name__}.") from None

    async def stop(self) -> None:
        """Delete the sandbox once and close the clients. Safe to call again."""
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._teardown())
        await asyncio.shield(self._stop_task)

    async def _teardown(self) -> None:
        """End a running start, delete the sandbox and close the clients."""
        if self._starting is not None and not self._starting.done():
            # A stop during the start: end the create first, so no sandbox
            # appears after this teardown has looked for it.
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        management, runtime, path, uid = self._management, self._runtime, self._path, self._uid
        self._management = self._runtime = None
        self._path = self._uid = None
        _LIVE.discard(self)
        if management is None:
            return
        # How the sandbox ended decides its bill: "deleted" settles at the
        # measured runtime, "leaked" runs to its sbx timeout and settles at the
        # whole lifetime, "unknown" keeps the hold, "none" releases it. Each
        # branch sets its worst case first, so a cancel mid-delete never
        # releases the hold of a live sandbox. A delete that went out but
        # outlasts STOP_TIMEOUT gets DELETE_WAIT more before "leaked": only a
        # sandbox that is deleting and then gone counts as deleted.
        ended = "none"
        state = _DeleteState()
        try:
            if path is not None:
                ended = "leaked"
                if await _bounded(_delete(management, path, uid, state), "delete the sandbox") or (
                    (state.sent or state.on_its_way) and await _wait_gone(management, path, uid, state)
                ):
                    ended = "deleted"
                    self._stop_usage_clock()
            elif self._create_dispatched:
                ended = "unknown"
                deleted = await _bounded(self._delete_orphan(management, state), "delete the lost sandbox")
                if self._orphan_found:
                    ended = "leaked"
                    if deleted or (
                        (state.sent or state.on_its_way)
                        and self._orphan is not None
                        and await _wait_gone(management, *self._orphan, state)
                    ):
                        ended = "deleted"
                        self._stop_usage_clock()
        finally:
            if ended == "unknown" and self._orphan_found:
                # A cancel hit the delete of a sandbox the stop found, so it
                # exists and may run until its sbx timeout.
                ended = "leaked"
            usage, self._usage = self._usage, None
            try:
                # The bill closes first and a cancel cannot cut it short, so a
                # cancel during the slow client closes never skips it.
                if usage is not None:
                    await _uncancellable(asyncio.ensure_future(asyncio.to_thread(self._close_usage, usage, ended)))
            finally:
                # A cancel while the bill closes still closes the clients.
                if runtime is not None:
                    await _bounded(runtime.aclose(), "close the runtime client")
                await _bounded(management.aclose(), "close the management client")

    def _stop_usage_clock(self) -> None:
        """End the measured runtime now, before the client closes can wait."""
        if self._usage is not None:
            self._usage.stop_clock()

    def _close_usage(self, usage: SbxUsageReservation, ended: str) -> None:
        """Settle, release or keep the box's hold by how the sandbox ended.

        Args:
            usage: The box's usage reservation.
            ended: ``deleted``, ``leaked``, ``unknown`` or ``none``.
        """
        try:
            if ended == "none":
                usage.release()
            elif ended == "unknown":
                usage.pending()
            else:
                usage.settle()
        except Exception as error:
            logger.warning("sbx sandbox usage stays pending: %s", type(error).__name__)

    async def _delete_orphan(self, client: httpx.AsyncClient, state: _DeleteState) -> None:
        """Delete the sandbox of a create whose outcome is unknown.

        The list can show a new sandbox late, and a cancelled create can still
        finish on the server, so the list is polled for up to ``ORPHAN_WAIT``.

        Args:
            client: The management client.
            state: Updated as the delete of the found sandbox goes on.

        Raises:
            SandboxInfraError: The sandbox list is invalid.
            SandboxError: A request failed; see ``_call``.
        """
        try:
            async with asyncio.timeout(ORPHAN_WAIT):
                while True:
                    sandbox = await _find(client, lambda item: item.get("displayName") == self._display_name)
                    if sandbox is not None:
                        break
                    await asyncio.sleep(POLL_INTERVAL)
        except TimeoutError:
            logger.warning("sbx found no sandbox of the lost create in %s seconds.", ORPHAN_WAIT)
            return
        self._orphan_found = True
        uid = sandbox.get("uid")
        self._orphan = (_resource_path(sandbox.get("name"), "sandboxes"), uid if isinstance(uid, str) and uid else None)
        await _delete(client, *self._orphan, state)

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every live sandbox of this process that carries this job tag.

        Args:
            job_id: The job tag value.

        Returns:
            The number of running sandboxes that were stopped.
        """
        stopped = 0
        for env in [env for env in _LIVE if env.spec.tags.get(JOB_TAG) == job_id]:
            if env._path is not None:
                stopped += 1
            await env.stop()
        return stopped
