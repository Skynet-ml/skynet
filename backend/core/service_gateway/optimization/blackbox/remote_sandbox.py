"""Control parent-owned sandboxes from an isolated optimizer without provider keys."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any
from urllib.parse import urlsplit

import httpx

from ....billing import budgets
from ....billing.runtime import OperationCompletedError, UsagePendingError
from ....billing.signals import BudgetReached
from ....exceptions import InfrastructureInterruptionError, ServiceError
from .sandbox_contract import CommandResult, OutputSink, SandboxSpec

_ERRORS = {
    error.__name__: error
    for error in (
        BudgetReached,
        UsagePendingError,
        OperationCompletedError,
        budgets.BudgetConflictError,
        budgets.BudgetFencedError,
        budgets.BudgetInsufficientError,
        budgets.BudgetInFlightError,
        budgets.BudgetUnreconciledError,
        budgets.BudgetBoundExceededError,
        budgets.BudgetFundingLostError,
    )
}


def _raise_remote_error(value: Mapping[str, Any]) -> None:
    """Propagate typed parent control signals without treating a budget stop as a score.

    Args:
        value: JSON error frame from the authenticated parent.

    Raises:
        BaseException: The mapped budget signal or a generic service failure.
    """
    if value.get("failure_kind") == "infrastructure_interruption":
        raise InfrastructureInterruptionError(
            str(value.get("message") or "The parent sandbox transport was interrupted.")
        )
    name = value.get("error_type")
    message = value.get("message")
    error = _ERRORS.get(name, ServiceError) if isinstance(name, str) else ServiceError
    raise error(message if isinstance(message, str) else "The parent sandbox operation failed.")


class RemoteSandboxRuntime:
    """Send guest work to one trusted parent's separately authenticated control route."""

    injects_headers = False
    protected = True

    def __init__(
        self,
        gateway_url: str,
        control_token: str,
        *,
        client: httpx.Client | None = None,
        lifetime_ceiling_seconds: float | None = None,
    ) -> None:
        """Bind an opaque parent capability without loading credentials or settings.

        Args:
            gateway_url: Parent loopback HTTP origin, optionally ending in /v1.
            control_token: Separate sandbox control capability, never a provider key.
            client: Optional owned test transport; normal requests use short-lived clients.
            lifetime_ceiling_seconds: The parent broker's maximum box lifetime,
                from the trusted descriptor; ``None`` sends requested lifetimes unchanged.
        """
        url = urlsplit(gateway_url)
        if (
            url.scheme != "http"
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.path.rstrip("/") not in {"", "/v1"}
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ServiceError("Sandbox control requires the trusted parent's loopback origin.")
        if not control_token:
            raise ServiceError("Sandbox control requires its separate opaque capability.")
        self._url = f"{url.scheme}://{url.netloc}/v1/_sandbox"
        self._headers = {"Authorization": f"Bearer {control_token}"}
        self._client = client
        self._ceiling = lifetime_ceiling_seconds

    def _request(self, action: str, payload: Mapping[str, Any], *, timeout: float = 90) -> dict[str, Any]:
        """Send one control operation without hidden retries or redirecting credentials.

        Args:
            action: Broker operation name.
            payload: JSON guest arguments.
            timeout: Maximum wait for one control response.

        Returns:
            The broker's JSON result.
        """
        context = (
            nullcontext(self._client) if self._client is not None else httpx.Client(trust_env=False, timeout=timeout)
        )
        with context as client:
            try:
                response = client.post(
                    self._url, headers=self._headers, json={"action": action, "payload": dict(payload)}
                )
            except httpx.TransportError as error:
                raise InfrastructureInterruptionError("The parent sandbox transport was interrupted.") from error
            try:
                value = response.json()
            except ValueError as error:
                raise ServiceError("The parent returned an invalid sandbox response.") from error
            if not isinstance(value, dict):
                raise ServiceError("The parent returned an invalid sandbox response.")
            if not response.is_success or value.get("type") == "error":
                _raise_remote_error(value)
            return value

    def open(self, spec: SandboxSpec) -> RemoteSandboxSession:
        """Request a sandbox within the parent's fixed protected runtime profile.

        Args:
            spec: Requested guest lifetime and environment.

        Returns:
            A guest handle containing only an opaque broker identity.
        """
        # Openers ask for the worker's lifetime setting, but a run deadline
        # gives the parent broker a shorter maximum that it strictly enforces.
        lifetime = spec.lifetime_seconds if self._ceiling is None else min(spec.lifetime_seconds, self._ceiling)
        value = self._request(
            "open",
            {
                "request_id": spec.operation_key or uuid.uuid4().hex,
                "spec": {
                    "lifetime_seconds": lifetime,
                    "env": dict(spec.env),
                    "image": spec.image,
                    "vcpus": spec.vcpus,
                    "inject_headers": dict(spec.inject_headers),
                    "allowed_hosts": list(spec.allowed_hosts),
                },
            },
            timeout=max(90, lifetime + 60),
        )
        identity = value.get("sandbox_id")
        if not isinstance(identity, str) or not identity:
            raise ServiceError("The parent did not return a sandbox capability.")
        return RemoteSandboxSession(self, identity, lifetime)


class RemoteSandboxSession:
    """Proxy a guest sandbox while billing and provider credentials stay in its parent."""

    def __init__(self, runtime: RemoteSandboxRuntime, identity: str, lifetime_seconds: float) -> None:
        """Retain a parent-issued sandbox capability and its requested lifetime.

        Args:
            runtime: Authenticated parent transport.
            identity: Opaque broker capability.
            lifetime_seconds: Granted box lifetime; bounds command timeouts and
                the wait for parent control responses.
        """
        self._runtime = runtime
        self._identity = identity
        self._lifetime = lifetime_seconds
        self._closed = False

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write text files in the owned guest directory.

        Args:
            files: Relative guest paths and contents.
        """
        self._runtime._request("write", {"sandbox_id": self._identity, "files": dict(files)})

    def write_codex_requirements(self) -> None:
        """Ask the parent to write the root-owned Codex requirements file in this box.

        The request names only the box: the parent builds the root command
        and the file's content from its own data.
        """
        self._runtime._request("codex_requirements", {"sandbox_id": self._identity})

    def read_file(self, path: str) -> str | None:
        """Read a guest file without exposing the parent's filesystem.

        Args:
            path: Relative guest path.

        Returns:
            Text content or None when absent.
        """
        value = self._runtime._request("read", {"sandbox_id": self._identity, "path": path}).get("content")
        if value is not None and not isinstance(value, str):
            raise ServiceError("The parent returned invalid sandbox file content.")
        return value

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        """Consume parent NDJSON output while allowing mailbox callbacks to use control RPC.

        Args:
            command: Guest shell command.
            env: Optional guest environment.
            timeout_seconds: Command timeout; a longer one is cut to the box lifetime.
            on_output: Callback for stdout/stderr chunks after parent model interception.
            sudo: Refused; the child never sends a root command.

        Returns:
            The terminal command result received from the parent.

        Raises:
            ServiceError: When ``sudo`` is set or the stream ends without a terminal result.
        """
        if sudo:
            raise ServiceError("A child cannot run sandbox commands as root.")
        request = {
            "action": "run",
            "payload": {
                "sandbox_id": self._identity,
                "command": command,
                "env": dict(env or {}),
                # Callers derive timeouts from the lifetime they asked for, which
                # can exceed a clamped box; no command outlives its box anyway.
                "timeout_seconds": None if timeout_seconds is None else min(timeout_seconds, self._lifetime),
            },
        }
        context = (
            nullcontext(self._runtime._client)
            if self._runtime._client is not None
            else httpx.Client(trust_env=False, timeout=max(90, self._lifetime + 60))
        )
        try:
            with (
                context as client,
                client.stream("POST", self._runtime._url, headers=self._runtime._headers, json=request) as response,
            ):
                if not response.is_success:
                    response.read()
                    try:
                        value = response.json()
                    except ValueError as error:
                        raise ServiceError("The parent rejected the sandbox command.") from error
                    _raise_remote_error(value if isinstance(value, dict) else {})
                for line in response.iter_lines():
                    if not line:
                        continue
                    try:
                        frame = json.loads(line)
                    except ValueError as error:
                        raise ServiceError("The parent returned an invalid sandbox output frame.") from error
                    if not isinstance(frame, dict):
                        raise ServiceError("The parent returned an invalid sandbox output frame.")
                    if frame.get("type") == "error":
                        _raise_remote_error(frame)
                    if frame.get("type") == "output":
                        stream, data = frame.get("stream"), frame.get("data")
                        if stream not in {"stdout", "stderr"} or not isinstance(data, str):
                            raise ServiceError("The parent returned an invalid sandbox output chunk.")
                        if on_output is not None:
                            on_output(stream, data)
                    elif frame.get("type") == "result":
                        code, stdout, stderr, timed_out = (
                            frame.get(key) for key in ("exit_code", "stdout", "stderr", "timed_out")
                        )
                        if (
                            isinstance(code, bool)
                            or not isinstance(code, int)
                            or not isinstance(stdout, str)
                            or not isinstance(stderr, str)
                            or not isinstance(timed_out, bool)
                        ):
                            raise ServiceError("The parent returned an invalid sandbox command result.")
                        return CommandResult(exit_code=code, stdout=stdout, stderr=stderr, timed_out=timed_out)
                    else:
                        raise ServiceError("The parent returned an unknown sandbox output frame.")
        except httpx.TransportError as error:
            raise InfrastructureInterruptionError("The parent sandbox transport was interrupted.") from error
        raise InfrastructureInterruptionError(
            "The sandbox stream ended before its final result; uncertain work will be reconciled before recovery."
        )

    def close(self) -> None:
        """Request cleanup and actual-usage settlement once from the trusted parent."""
        if self._closed:
            return
        self._closed = True
        self._runtime._request("close", {"sandbox_id": self._identity}, timeout=max(90, self._lifetime + 60))
