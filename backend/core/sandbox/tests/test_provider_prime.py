"""Tests for the Prime Intellect sandbox provider, against an in-memory fake of the SDK.

No test reaches Prime: an autouse fixture swaps the SDK client, its request
model and its error classes for fakes, so the suite needs no network, no API
key and no ``prime`` extra.
"""

from __future__ import annotations

import asyncio
import itertools
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing.prime_usage import quote_prime_sandbox
from core.billing.runtime import UsagePendingError
from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import (
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from core.sandbox.metering import usage_adapter
from core.sandbox.providers import prime
from core.sandbox.providers.prime import BOX_TAG, PrimeEnvironment
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.timeouts import PhaseTimeouts

from .test_environment import SandboxEnvironmentContract


class FakeAPIError(Exception):
    """Stand in for ``prime_sandboxes.APIError``."""


class FakeUnauthorizedError(FakeAPIError):
    """Stand in for ``UnauthorizedError``."""


class FakePaymentRequiredError(FakeAPIError):
    """Stand in for ``PaymentRequiredError``."""


class FakeAPITimeoutError(FakeAPIError):
    """Stand in for ``APITimeoutError``."""


class FakeFileNotFoundError(FakeAPIError):
    """Stand in for ``SandboxFileNotFoundError``."""


class FakeSandboxNotRunningError(RuntimeError):
    """Stand in for ``SandboxNotRunningError``."""


class FakeCommandTimeoutError(RuntimeError):
    """Stand in for ``CommandTimeoutError``."""


class FakeUploadTimeoutError(RuntimeError):
    """Stand in for ``UploadTimeoutError``."""


# The SDK model field defaults, before its validator runs: the fake runs no validator.
_SDK_DEFAULTS = {"cpu_cores": 1.0, "memory_gb": 1.0, "disk_size_gb": None, "timeout_minutes": 60, "gpu_count": 0}
_CREATED_AT = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
_FAKE_ENDED = frozenset({"ERROR", "TERMINATED", "TIMEOUT"})


class FakeCreateRequest(dict):
    """Stand in for ``CreateSandboxRequest``: keep the fields as a dict."""

    def __init__(self, **fields: Any) -> None:
        """Keep the request fields.

        Args:
            **fields: The request fields.
        """
        super().__init__(fields)

    def __getattr__(self, name: str) -> Any:
        """Read a field like the SDK model, with the SDK default when it is not set.

        Args:
            name: The field name.

        Returns:
            The field value.
        """
        if name in self:
            return self[name]
        if name in _SDK_DEFAULTS:
            return _SDK_DEFAULTS[name]
        raise AttributeError(name)


@dataclass
class BoxState:
    """Hold one fake sandbox in the fake cloud."""

    id: str
    labels: list[str]
    request: dict[str, Any]
    status: str = "PENDING"
    terminated_at: datetime | None = None
    files: dict[str, str] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=lambda: {"/", "/tmp"})


@dataclass
class FakeCloud:
    """Hold the shared state of the fake Prime account."""

    boxes: dict[str, BoxState] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    clients: list[dict[str, Any]] = field(default_factory=list)
    exec_calls: list[dict[str, Any]] = field(default_factory=list)
    network_calls: list[dict[str, Any]] = field(default_factory=list)
    list_calls: list[dict[str, Any]] = field(default_factory=list)
    create_error: BaseException | None = None
    create_hangs: bool = False
    wait_hangs: bool = False
    exec_hangs: bool = False
    exec_error: BaseException | None = None
    upload_error: BaseException | None = None
    network_error: BaseException | None = None
    list_hangs: bool = False
    list_ignores_labels: bool = False
    page_size: int = 100
    # The get_network calls before Prime reports the policy as applied; None never applies it.
    apply_after: int | None = 0
    ids: Any = field(default_factory=lambda: itertools.count(1))
    # The seconds between the create and the end that the delete records; None records no end.
    run_seconds: float | None = 90.0

    def run(self, box: BoxState, command: str, env: dict[str, str] | None) -> tuple[int, str, str]:
        """Run one raw command in a tiny shell model.

        Args:
            box: The sandbox.
            command: The command that ``execute_command`` received.
            env: The per-command environment.

        Returns:
            The exit code, stdout and stderr.
        """
        variables = {**box.request.get("secrets", {}), **(env or {})}
        words = shlex.split(command)
        verb, args = words[0], words[1:]
        if verb == "true":
            return 0, "", ""
        if verb == "false":
            return 1, "", ""
        if verb == "exit":
            return int(args[0]), "", ""
        if verb == "echo":
            return 0, " ".join(args) + "\n", ""
        if verb == "printenv":
            return (0, variables[args[0]] + "\n", "") if args[0] in variables else (1, "", "")
        if verb == "cat":
            return (0, box.files[args[0]], "") if args[0] in box.files else (1, "", "No such file\n")
        if verb == "mkdir" and args[0] == "-p":
            for path in args[1:]:
                parts = path.strip("/").split("/")
                box.dirs.update("/" + "/".join(parts[: n + 1]) for n in range(len(parts)))
            return 0, "", ""
        return 127, "", f"{verb}: command not found\n"


CLOUD = FakeCloud()


class FakeClient:
    """Stand in for ``prime_sandboxes.AsyncSandboxClient``."""

    def __init__(self, **kwargs: Any) -> None:
        """Record the client options.

        Args:
            **kwargs: The client constructor options.
        """
        CLOUD.clients.append(kwargs)

    async def create(self, request: FakeCreateRequest) -> SimpleNamespace:
        """Create a sandbox.

        Args:
            request: The create request.

        Returns:
            The sandbox view.
        """
        CLOUD.calls.append("create")
        if CLOUD.create_error is not None:
            raise CLOUD.create_error
        box = BoxState(id=f"sbx-{next(CLOUD.ids)}", labels=list(request["labels"]), request=dict(request))
        CLOUD.boxes[box.id] = box
        if CLOUD.create_hangs:
            await asyncio.Event().wait()
        return self._view(box)

    @staticmethod
    def _view(box: BoxState) -> SimpleNamespace:
        """Return the SDK ``Sandbox`` view of a fake sandbox.

        Args:
            box: The sandbox.

        Returns:
            An object like ``Sandbox``.
        """
        sizes = {name: box.request.get(name, default) for name, default in _SDK_DEFAULTS.items()}
        return SimpleNamespace(
            id=box.id,
            status=box.status,
            created_at=_CREATED_AT,
            started_at=None,
            terminated_at=box.terminated_at,
            **sizes,
        )

    async def get(self, sandbox_id: str) -> SimpleNamespace:
        """Read one sandbox.

        Args:
            sandbox_id: The sandbox id.

        Returns:
            An object like ``Sandbox``.
        """
        CLOUD.calls.append(f"get:{sandbox_id}")
        return self._view(CLOUD.boxes[sandbox_id])

    async def wait_for_creation(self, sandbox_id: str, *, max_attempts: int) -> None:
        """Wait until the sandbox runs, forever when the cloud says it hangs.

        Args:
            sandbox_id: The sandbox id.
            max_attempts: The SDK wait budget.
        """
        assert max_attempts == prime._UNBOUNDED_WAIT
        if CLOUD.wait_hangs:
            await asyncio.Event().wait()
        CLOUD.boxes[sandbox_id].status = "RUNNING"

    # The fake keeps the SDK signature, so it takes a `timeout` parameter.
    async def execute_command(
        self,
        sandbox_id: str,
        command: str,
        *,
        working_dir: str | None,
        env: dict[str, str] | None,
        timeout: int,  # noqa: ASYNC109
    ) -> SimpleNamespace:
        """Run one command and return its captured output.

        Args:
            sandbox_id: The sandbox id.
            command: The raw command.
            working_dir: The working directory.
            env: The per-command environment.
            timeout: The SDK timeout in seconds.

        Returns:
            An object like ``CommandResponse``.
        """
        CLOUD.exec_calls.append({"command": command, "working_dir": working_dir, "env": env, "timeout": timeout})
        if CLOUD.exec_error is not None:
            raise CLOUD.exec_error
        if CLOUD.exec_hangs:
            await asyncio.Event().wait()
        code, out, err = CLOUD.run(CLOUD.boxes[sandbox_id], command, env)
        return SimpleNamespace(exit_code=code, stdout=out, stderr=err)

    async def upload_bytes(self, sandbox_id: str, path: str, data: bytes, filename: str) -> None:
        """Write a file whose parent directory exists.

        Args:
            sandbox_id: The sandbox id.
            path: The absolute path.
            data: The file bytes.
            filename: The file name.

        Raises:
            FakeAPIError: The parent directory does not exist.
        """
        if CLOUD.upload_error is not None:
            raise CLOUD.upload_error
        box = CLOUD.boxes[sandbox_id]
        assert filename == PurePosixPath(path).name
        if str(PurePosixPath(path).parent) not in box.dirs:
            raise FakeAPIError("HTTP 400: no parent directory")
        box.files[path] = data.decode("utf-8")

    async def read_file(self, sandbox_id: str, path: str) -> SimpleNamespace:
        """Return the file text.

        Args:
            sandbox_id: The sandbox id.
            path: The absolute path.

        Returns:
            An object like ``ReadFileResponse``.

        Raises:
            FakeFileNotFoundError: The file does not exist.
        """
        box = CLOUD.boxes[sandbox_id]
        if path not in box.files:
            raise FakeFileNotFoundError(path)
        return SimpleNamespace(content=box.files[path])

    async def set_network(
        self, sandbox_id: str, *, allow: list[str] | None = None, deny: list[str] | None = None
    ) -> SimpleNamespace:
        """Record the new egress rules.

        Args:
            sandbox_id: The sandbox id.
            allow: The allowed hosts.
            deny: The denied hosts.

        Returns:
            An object like ``EgressPolicyStatus``.
        """
        if CLOUD.network_error is not None:
            raise CLOUD.network_error
        CLOUD.network_calls.append({"id": sandbox_id, "allow": allow, "deny": deny})
        return SimpleNamespace(applied=CLOUD.apply_after == 0)

    async def get_network(self, sandbox_id: str) -> SimpleNamespace:
        """Report whether Prime applied the egress rules.

        Args:
            sandbox_id: The sandbox id.

        Returns:
            An object like ``EgressPolicyStatus``.
        """
        CLOUD.calls.append("get_network")
        if CLOUD.apply_after is None:
            return SimpleNamespace(applied=False)
        CLOUD.apply_after -= 1
        return SimpleNamespace(applied=CLOUD.apply_after <= 0)

    async def list(
        self, *, labels: list[str], exclude_terminated: bool | None, per_page: int, cursor: str | None
    ) -> SimpleNamespace:
        """Return one page of sandboxes with these labels.

        Args:
            labels: The label filter.
            exclude_terminated: The SDK ``is_active`` filter, or ``None`` for every box.
            per_page: The page size the provider asked for.
            cursor: The page cursor.

        Returns:
            An object like ``SandboxListResponse``.
        """
        CLOUD.list_calls.append(
            {"labels": labels, "per_page": per_page, "cursor": cursor, "exclude_terminated": exclude_terminated}
        )
        # Like the SDK: None sends no is_active and lists every box; True
        # lists only active boxes; False lists only ended boxes.
        boxes = [
            box
            for box in CLOUD.boxes.values()
            if (CLOUD.list_ignores_labels or set(labels) <= set(box.labels))
            and (exclude_terminated is None or (box.status not in _FAKE_ENDED) == exclude_terminated)
        ]
        start = int(cursor or 0)
        page = boxes[start : start + CLOUD.page_size]
        has_next = start + CLOUD.page_size < len(boxes)
        if CLOUD.list_hangs and not has_next:
            await asyncio.Event().wait()
        return SimpleNamespace(
            sandboxes=[SimpleNamespace(id=box.id, labels=list(box.labels), status=box.status) for box in page],
            has_next=has_next,
            next_cursor=str(start + CLOUD.page_size) if has_next else None,
        )

    async def delete(self, sandbox_id: str) -> None:
        """Delete a sandbox by id.

        Args:
            sandbox_id: The sandbox id.
        """
        CLOUD.calls.append(f"delete:{sandbox_id}")
        box = CLOUD.boxes[sandbox_id]
        box.status = "TERMINATED"
        if CLOUD.run_seconds is not None:
            box.terminated_at = _CREATED_AT + timedelta(seconds=CLOUD.run_seconds)

    async def aclose(self) -> None:
        """Close the client."""
        CLOUD.calls.append("aclose")


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> FakeCloud:
    """Swap the Prime SDK for the fake and reset the fake cloud.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fresh fake cloud.
    """
    global CLOUD
    CLOUD = FakeCloud()
    fakes = {
        "AsyncSandboxClient": FakeClient,
        "CreateSandboxRequest": FakeCreateRequest,
        "APIError": FakeAPIError,
        "APITimeoutError": FakeAPITimeoutError,
        "UnauthorizedError": FakeUnauthorizedError,
        "PaymentRequiredError": FakePaymentRequiredError,
        "SandboxFileNotFoundError": FakeFileNotFoundError,
        "SandboxNotRunningError": FakeSandboxNotRunningError,
        "CommandTimeoutError": FakeCommandTimeoutError,
        "UploadTimeoutError": FakeUploadTimeoutError,
    }
    for name, value in fakes.items():
        monkeypatch.setattr(prime, name, value)
    monkeypatch.setenv(prime.API_KEY_ENV, "fake-key-for-tests")
    monkeypatch.setattr(prime, "STOP_TIMEOUT", 1.0)
    monkeypatch.setattr(prime, "POLL_INTERVAL", 0.001)
    monkeypatch.setattr(prime, "SEARCH_AGAIN_DELAY", 0.0)
    return CLOUD


class TestPrimeEnvironmentContract(SandboxEnvironmentContract):
    """Run the shared provider contract against the fake Prime cloud."""

    provider_cls = PrimeEnvironment


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small spec.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), **overrides})


def _timeouts(*, start: float = 30, run: float = 30) -> PhaseTimeouts:
    """Return phase timeouts with these start and run limits.

    Args:
        start: The start limit in seconds.
        run: The run limit in seconds.

    Returns:
        The phase timeouts.
    """
    return PhaseTimeouts(start=start, setup=30, run=run, score=30)


def _fields(spec: SandboxSpec) -> dict[str, Any]:
    """Return the create request fields that the provider builds for a spec.

    Args:
        spec: The spec.

    Returns:
        The create request fields.
    """
    return PrimeEnvironment(spec)._create_fields


def test_prime_is_not_allowed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the Prime provider in production before its import (D1)."""
    assert factory.PROVIDERS["prime"].production_allowed is False
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(factory.ProductionNotAllowedError, match="prime"):
        factory.load_provider("prime")


def test_factory_loads_prime_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the Prime class from the registry outside production."""
    monkeypatch.setattr(factory.settings, "app_environment", "staging")
    assert factory.load_provider("prime") is PrimeEnvironment


def test_preflight_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the extra when the SDK is not installed."""
    monkeypatch.setattr(prime, "AsyncSandboxClient", None)
    with pytest.raises(SandboxFatalError, match="`prime` extra"):
        PrimeEnvironment.preflight()


def test_preflight_needs_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the variable when the API key is not set."""
    monkeypatch.delenv(prime.API_KEY_ENV)
    with pytest.raises(SandboxFatalError, match=prime.API_KEY_ENV):
        PrimeEnvironment.preflight()


async def test_the_client_gets_the_key_from_the_environment(fake_sdk: FakeCloud) -> None:
    """Pass the key, so the SDK never reads a local config file."""
    async with PrimeEnvironment(_spec()):
        pass
    assert fake_sdk.clients == [{"api_key": "fake-key-for-tests"}]


async def test_a_transient_create_error_is_raised_once(fake_sdk: FakeCloud) -> None:
    """Raise a transient error after one create call, with no retry (D3, D10)."""
    fake_sdk.create_error = FakeAPIError("HTTP 429: slow down")
    with pytest.raises(SandboxTransientError):
        async with PrimeEnvironment(_spec()):
            pass
    assert fake_sdk.calls.count("create") == 1
    assert fake_sdk.calls[-1] == "aclose"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeUnauthorizedError("HTTP 401: bad key"), SandboxFatalError),
        (FakePaymentRequiredError("HTTP 402: no funds"), SandboxFatalError),
        (FakeAPITimeoutError("Request timed out"), SandboxTransientError),
        (FakeAPIError("Request failed: connection reset"), SandboxTransientError),
        (FakeAPIError("HTTP 429: slow down"), SandboxTransientError),
        (FakeAPIError("HTTP 503: unavailable"), SandboxTransientError),
        (FakeAPIError("HTTP 400: bad image"), SandboxRejectedError),
        (FakeAPIError("HTTP 404: no such thing"), SandboxRejectedError),
        (FakeAPIError("HTTP 409: conflict"), SandboxRejectedError),
        (FakeAPIError("HTTP 422: bad field"), SandboxRejectedError),
        (FakeAPIError("HTTP 403: forbidden"), SandboxInfraError),
        (FakeAPIError("Upload failed: HTTP 503 PUT https://gw/upload: unavailable"), SandboxTransientError),
        (FakeAPIError("Upload failed: HTTP 503: unavailable"), SandboxTransientError),
        (FakeAPIError("Upload failed: ConnectError at PUT https://gw/upload: refused"), SandboxTransientError),
        (FakeAPIError("Upload failed after retries"), SandboxTransientError),
        (FakeAPIError("Upload failed: HTTP 401 PUT https://gw/upload: Unauthorized"), SandboxFatalError),
        (FakeAPIError("Upload failed: HTTP 402: Payment Required"), SandboxFatalError),
        (FakeAPIError("Upload failed: HTTP 409 PUT https://gw/upload: busy"), SandboxRejectedError),
        (FakeAPIError("Read file failed: HTTP 502 GET https://gw/read-file: Bad Gateway"), SandboxTransientError),
        (FakeAPIError("Read file timed out after 30s (ReadTimeout): /x"), SandboxTransientError),
        (FakeAPIError("Read file failed at GET https://gw/read-file: ConnectError"), SandboxTransientError),
        (FakeAPIError("something else"), SandboxInfraError),
        (FakeSandboxNotRunningError("gone"), SandboxInfraError),
    ],
)
async def test_sdk_errors_map_to_sandbox_errors(
    fake_sdk: FakeCloud, error: Exception, expected: type[Exception]
) -> None:
    """Map each SDK error group to its sandbox error and keep the cause."""
    fake_sdk.create_error = error
    with pytest.raises(expected) as caught:
        await PrimeEnvironment(_spec()).start()
    assert caught.value.__cause__ is error


def test_create_fields_carry_the_labels_secrets_and_sizes() -> None:
    """Label the sandbox, send the env as secrets and convert the sizes."""
    spec = _spec(
        name="trial",
        image="python:3.11",
        tags={JOB_TAG: "job-1"},
        env={"A": "1"},
        lifetime_seconds=125,
        resources=ResourceRequest(cpus=2, memory_mb=3072, disk_mb=10240),
    )
    env = PrimeEnvironment(spec)
    fields = env._create_fields
    assert fields["labels"] == [f"{BOX_TAG}={env._box_tag}", f"{JOB_TAG}=job-1"]
    assert fields["idempotency_key"] == env._box_tag
    assert fields["name"] == "trial"
    assert fields["docker_image"] == "python:3.11"
    assert fields["timeout_minutes"] == 3
    assert fields["secrets"] == {"A": "1"}
    assert (fields["cpu_cores"], fields["memory_gb"], fields["disk_size_gb"]) == (2.0, 3.0, 10.0)
    assert fields["network_allowlist"] is None


def test_create_fields_defaults() -> None:
    """Use the default image, a short name and no secrets for a bare spec."""
    env = PrimeEnvironment(_spec(lifetime_seconds=1))
    fields = env._create_fields
    assert fields["docker_image"] == prime.DEFAULT_IMAGE == "python:3.12.15-slim"
    assert fields["name"] == f"skynet-{env._box_tag[:12]}"
    assert fields["timeout_minutes"] == 1
    assert "secrets" not in fields
    assert "memory_gb" not in fields
    assert fields["disk_size_gb"] == prime.DEFAULT_DISK_GB == 5.0
    assert len(_fields(_spec(name="x" * 300))["name"]) == prime._NAME_LIMIT


def test_ignore_mode_sends_only_the_default_disk() -> None:
    """Send no CPU or memory size when the spec ignores resources, and the SDK's default disk."""
    fields = _fields(
        _spec(resources=ResourceRequest(cpus=64, memory_mb=1024, disk_mb=99_999, mode=ResourceMode.IGNORE))
    )
    assert not {"cpu_cores", "memory_gb"} & fields.keys()
    assert fields["disk_size_gb"] == prime.DEFAULT_DISK_GB


def test_create_fields_map_the_network_mode() -> None:
    """Send an empty allowlist for no egress, and the deduped hosts for an allowlist."""
    assert _fields(_spec(network_disabled=True))["network_allowlist"] == []
    hosts = ("pypi.org", "files.pythonhosted.org", "pypi.org")
    assert _fields(_spec(allowed_hosts=hosts))["network_allowlist"] == ["pypi.org", "files.pythonhosted.org"]


async def test_disable_network_denies_all_egress(fake_sdk: FakeCloud) -> None:
    """Deny every host and wait until Prime applies the rules."""
    fake_sdk.apply_after = 2
    async with PrimeEnvironment(_spec()) as env:
        await env.disable_network()
        assert env.network_policy == NetworkPolicy(disabled=True)
    assert fake_sdk.network_calls[0]["deny"] == ["*"]
    assert fake_sdk.calls.count("get_network") == 2


async def test_allowlist_and_open_policies_send_allow_rules(fake_sdk: FakeCloud) -> None:
    """Allow only the listed hosts, then every host for open egress."""
    async with PrimeEnvironment(_spec()) as env:
        await env._apply_network_policy(NetworkPolicy(allowed_hosts=("a.example", "b.example", "a.example")))
        await env._apply_network_policy(NetworkPolicy())
    assert [call["allow"] for call in fake_sdk.network_calls] == [["a.example", "b.example"], ["*"]]


async def test_a_policy_that_never_applies_is_an_infra_error(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise an infra error when Prime does not apply the rules in time."""
    monkeypatch.setattr(prime, "NETWORK_APPLY_TIMEOUT", 0.05)
    fake_sdk.apply_after = None
    async with PrimeEnvironment(_spec()) as env:
        with pytest.raises(SandboxInfraError, match="in time"):
            await env._apply_network_policy(NetworkPolicy(disabled=True))


async def test_a_second_start_is_refused(fake_sdk: FakeCloud) -> None:
    """Refuse a second start, while the sandbox runs and after the stop."""
    env = PrimeEnvironment(_spec())
    await env.start()
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()
    await env.stop()
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()
    assert fake_sdk.calls.count("create") == 1


async def test_a_stop_before_the_start_still_lets_the_sandbox_stop(fake_sdk: FakeCloud) -> None:
    """Start after an early stop, then delete the sandbox at the next stop."""
    env = PrimeEnvironment(_spec())
    await env.stop()
    await env.start()
    await env.stop()
    (box,) = fake_sdk.boxes.values()
    assert box.status == "TERMINATED"


async def test_exec_passes_cwd_env_and_the_sdk_timeout(fake_sdk: FakeCloud) -> None:
    """Send the raw command with its cwd, env and a whole-second SDK timeout."""
    async with PrimeEnvironment(_spec()) as env:
        result = await env.exec("printenv X", cwd="/tmp", env={"X": "a b'c"}, timeout_sec=2.5)
        await env.exec("true")
    assert result.stdout == "a b'c\n"
    assert fake_sdk.exec_calls[-2] == {
        "command": "printenv X",
        "working_dir": "/tmp",
        "env": {"X": "a b'c"},
        "timeout": 3,
    }
    assert fake_sdk.exec_calls[-1]["timeout"] == prime._NO_LIMIT_SECONDS


async def test_exec_sees_the_start_env(fake_sdk: FakeCloud) -> None:
    """Make the spec env, sent as secrets, visible to a command."""
    async with PrimeEnvironment(_spec(env={"START": "yes"})) as env:
        assert (await env.exec("printenv START")).stdout == "yes\n"


async def test_exec_rejects_a_bad_env_name() -> None:
    """Reject an env name that the shell cannot set."""
    async with PrimeEnvironment(_spec()) as env:
        with pytest.raises(SandboxRejectedError):
            await env.exec("true", env={"A;rm": "x"})


async def test_exec_before_start_is_an_infra_error() -> None:
    """Refuse a command before the sandbox runs."""
    with pytest.raises(SandboxInfraError, match="not running"):
        await PrimeEnvironment(_spec()).exec("true")


async def test_a_timed_out_command_returns_a_timed_out_result(fake_sdk: FakeCloud) -> None:
    """Return a timed-out result at the local time limit."""
    async with PrimeEnvironment(_spec()) as env:
        fake_sdk.exec_hangs = True
        result = await env.exec("true", timeout_sec=0.05)
        assert (result.exit_code, result.timed_out) == (124, True)
    assert fake_sdk.exec_calls[-1]["timeout"] == 1


async def test_the_run_timeout_is_the_default_limit(fake_sdk: FakeCloud) -> None:
    """Use the run phase timeout when the call sets no limit."""
    async with PrimeEnvironment(_spec(), timeouts=_timeouts(run=0.05)) as env:
        fake_sdk.exec_hangs = True
        result = await env.exec("true")
        assert (result.exit_code, result.timed_out) == (124, True)


async def test_an_sdk_command_timeout_returns_a_timed_out_result(fake_sdk: FakeCloud) -> None:
    """Turn the SDK command timeout into a timed-out result."""
    async with PrimeEnvironment(_spec()) as env:
        fake_sdk.exec_error = FakeCommandTimeoutError("timed out")
        result = await env.exec("true", timeout_sec=5)
        assert (result.exit_code, result.timed_out) == (124, True)


async def test_exec_sends_output_after_the_command(fake_sdk: FakeCloud) -> None:
    """Send stdout and stderr to the sink, and skip an empty stream."""
    chunks: list[tuple[str, str]] = []
    async with PrimeEnvironment(_spec()) as env:
        await env.exec("nope", on_output=lambda stream, text: chunks.append((stream, text)))
        await env.exec("true", on_output=lambda stream, text: chunks.append((stream, text)))
    assert chunks == [("stderr", "nope: command not found\n")]


async def test_upload_makes_the_parent_directories(fake_sdk: FakeCloud) -> None:
    """Make every parent directory in one command before the writes."""
    async with PrimeEnvironment(_spec()) as env:
        await env.upload_files({"/w/a/x.txt": "x", "/w/b/y.txt": "y", "/top.txt": "t"})
        assert await env.download_file("/w/b/y.txt") == "y"
        assert await env.download_file("/top.txt") == "t"
    assert sum(call["command"].startswith("mkdir -p") for call in fake_sdk.exec_calls) == 1


async def test_start_timeout_deletes_the_sandbox(fake_sdk: FakeCloud) -> None:
    """Raise StartTimeoutError and delete the sandbox that did not reach running."""
    fake_sdk.wait_hangs = True
    env = PrimeEnvironment(_spec(), timeouts=_timeouts(start=0.05))
    with pytest.raises(StartTimeoutError):
        await env.start()
    (box,) = fake_sdk.boxes.values()
    assert box.status == "TERMINATED"
    assert fake_sdk.calls[-1] == "aclose"


async def test_start_timeout_during_the_create_deletes_the_lost_sandbox(fake_sdk: FakeCloud) -> None:
    """Find the sandbox of a create that never returned by its box label and delete it."""
    other = PrimeEnvironment(_spec())
    await other.start()
    fake_sdk.create_hangs = True
    with pytest.raises(StartTimeoutError):
        await PrimeEnvironment(_spec(), timeouts=_timeouts(start=0.05)).start()
    assert [box.status for box in fake_sdk.boxes.values()] == ["RUNNING", "TERMINATED"]
    await other.stop()


async def test_stop_during_start_deletes_the_sandbox(fake_sdk: FakeCloud) -> None:
    """End a running start from another task and leak no sandbox."""
    fake_sdk.create_hangs = True
    env = PrimeEnvironment(_spec())
    start = asyncio.ensure_future(env.start())
    await asyncio.sleep(0.01)
    await env.stop()
    with pytest.raises(SandboxInfraError, match="during its start"):
        await start
    (box,) = fake_sdk.boxes.values()
    assert box.status == "TERMINATED"


async def test_cancelled_start_deletes_the_sandbox(fake_sdk: FakeCloud) -> None:
    """Keep the cancel and leak no sandbox when the caller cancels the start."""
    fake_sdk.wait_hangs = True
    start = asyncio.ensure_future(PrimeEnvironment(_spec()).start())
    await asyncio.sleep(0.01)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    (box,) = fake_sdk.boxes.values()
    assert box.status == "TERMINATED"


async def test_a_failed_create_deletes_nothing(fake_sdk: FakeCloud) -> None:
    """Look for a lost sandbox by label after a failed create, and find none."""
    fake_sdk.create_error = FakeAPIError("HTTP 400: bad image")
    with pytest.raises(SandboxRejectedError):
        await PrimeEnvironment(_spec()).start()
    assert not any(call.startswith("delete:") for call in fake_sdk.calls)
    assert fake_sdk.list_calls[0]["labels"][0].startswith(f"{BOX_TAG}=")


async def test_stop_job_sandboxes_pages_and_skips_other_sandboxes(fake_sdk: FakeCloud) -> None:
    """Read every page and delete only the active sandboxes of the job."""
    fake_sdk.page_size = 2
    envs = [PrimeEnvironment(_spec(tags={JOB_TAG: "job-9"})) for _ in range(4)]
    other = PrimeEnvironment(_spec(tags={JOB_TAG: "job-other"}))
    for env in (*envs, other):
        await env.start()
    await envs[0].stop()
    fake_sdk.boxes[envs[1]._sandbox_id].status = "ERROR"
    fake_sdk.list_ignores_labels = True
    assert await PrimeEnvironment.stop_job_sandboxes("job-9") == 2
    job_calls = [call for call in fake_sdk.list_calls if call["labels"] == [f"{JOB_TAG}=job-9"]]
    assert [call["cursor"] for call in job_calls] == [None, "2"]
    assert all(call["per_page"] == prime._LIST_PAGE for call in job_calls)
    assert fake_sdk.boxes[other._sandbox_id].status == "RUNNING"
    assert fake_sdk.calls[-1] == "aclose"
    for env in (*envs, other):
        await env.stop()


async def test_stop_job_sandboxes_bounds_the_list(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete the sandboxes found before a list that runs past the limit."""
    monkeypatch.setattr(prime, "STOP_TIMEOUT", 0.05)
    fake_sdk.page_size = 1
    envs = [PrimeEnvironment(_spec(tags={JOB_TAG: "job-5"})) for _ in range(2)]
    for env in envs:
        await env.start()
    fake_sdk.list_hangs = True
    assert await PrimeEnvironment.stop_job_sandboxes("job-5") == 1
    assert fake_sdk.calls[-1] == "aclose"
    for env in envs:
        await env.stop()


async def test_stop_job_sandboxes_counts_only_successful_deletes(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave a failed delete out of the count and go on."""
    envs = [PrimeEnvironment(_spec(tags={JOB_TAG: "job-7"})) for _ in range(2)]
    for env in envs:
        await env.start()
    calls = 0
    original = FakeClient.delete

    async def flaky(self: FakeClient, sandbox_id: str) -> None:
        """Fail the first delete only."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FakeAPIError("HTTP 500: oops")
        await original(self, sandbox_id)

    monkeypatch.setattr(FakeClient, "delete", flaky)
    assert await PrimeEnvironment.stop_job_sandboxes("job-7") == 1
    for env in envs:
        await env.stop()


_SECRET = "sk-secret-value-for-tests-123"


def _assert_no_secret(error: BaseException) -> None:
    """Check that no text of the error, its cause or its context holds the secret.

    Args:
        error: The raised error.
    """
    assert error.__cause__ is None
    assert error.__context__ is None
    assert _SECRET not in str(error)
    assert _SECRET not in repr(error)


async def test_a_create_request_the_sdk_rejects_is_a_spec_error_without_secrets(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raise a spec error that shows no secret and send no create (review finding B)."""

    def reject(**fields: Any) -> None:
        """Fail like pydantic, with the input values in the message."""
        raise ValueError(f"1 validation error for CreateSandboxRequest: input={fields!r}")

    monkeypatch.setattr(prime, "CreateSandboxRequest", reject)
    with pytest.raises(SandboxSpecError) as caught:
        await PrimeEnvironment(_spec(env={"OPENAI_API_KEY": _SECRET})).start()
    _assert_no_secret(caught.value)
    assert "create" not in fake_sdk.calls
    assert fake_sdk.list_calls == []
    assert fake_sdk.calls[-1] == "aclose"


@pytest.mark.parametrize("host", ["api.example.com:8443", "https://api.example.com", "*"])
async def test_the_real_sdk_model_rejection_hides_the_secrets(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    """Map the real pydantic rejection of an allowlist host to a clean spec error."""
    sdk = pytest.importorskip("prime_sandboxes")
    monkeypatch.setattr(prime, "CreateSandboxRequest", sdk.CreateSandboxRequest)
    with pytest.raises(SandboxSpecError) as caught:
        await PrimeEnvironment(_spec(env={"OPENAI_API_KEY": _SECRET}, allowed_hosts=(host,))).start()
    _assert_no_secret(caught.value)
    assert "create" not in fake_sdk.calls


async def test_a_policy_host_the_sdk_rejects_is_a_spec_error(fake_sdk: FakeCloud) -> None:
    """Map the SDK ValueError for a policy host to a spec error (review finding C)."""
    async with PrimeEnvironment(_spec()) as env:
        fake_sdk.network_error = ValueError("'*' must be the only destination")
        with pytest.raises(SandboxSpecError) as caught:
            await env.set_network_policy(NetworkPolicy(allowed_hosts=("*", "a.example")))
        assert caught.value.__cause__ is None
        assert env.network_policy is None


async def test_an_upload_timeout_is_transient(fake_sdk: FakeCloud) -> None:
    """Map the SDK upload timeout to a transient error and keep the cause (review finding D)."""
    async with PrimeEnvironment(_spec()) as env:
        error = FakeUploadTimeoutError("upload timed out")
        fake_sdk.upload_error = error
        with pytest.raises(SandboxTransientError) as caught:
            await env.upload_files({"/tmp/a.txt": "x"})
    assert caught.value.__cause__ is error


@dataclass
class FakeUsage:
    """Record the reservation calls that a protected sandbox makes."""

    budget: Any
    request: dict[str, Any]
    operation_key: str
    created_before: list[str]
    calls: list[tuple[str, Any]] = field(default_factory=list)
    sandbox_id: str | None = None
    settle_error: Exception | None = None
    seconds: list[int | None] = field(default_factory=list)

    def confirm_created(self, record: dict[str, Any]) -> None:
        """Bind the created sandbox.

        Args:
            record: The sandbox record.
        """
        self.sandbox_id = record["id"]
        self.calls.append(("confirm", record))

    def settle(self, record: dict[str, Any] | None = None, *, seconds: int | None = None) -> None:
        """Record the settle, or fail it.

        Args:
            record: The final sandbox record.
            seconds: The lifetime to charge when the record has no end.

        Raises:
            Exception: The ``settle_error`` the test set.
        """
        self.calls.append(("settle", record))
        self.seconds.append(seconds)
        if self.settle_error is not None:
            raise self.settle_error

    def pending(self, record: dict[str, Any] | None = None) -> None:
        """Record that the hold stays pending.

        Args:
            record: The last sandbox record.
        """
        self.calls.append(("pending", record))

    def release(self, reason: str) -> None:
        """Record the release of a hold with no sandbox.

        Args:
            reason: Why no sandbox exists.
        """
        self.calls.append(("release", reason))


class UsageRecorder:
    """Make fake reservations and keep each one."""

    def __init__(self, settle_error: Exception | None = None) -> None:
        """Start with no reservation.

        Args:
            settle_error: The error each settle raises, or ``None``.
        """
        self.made: list[FakeUsage] = []
        self.settle_error = settle_error

    def __call__(self, budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make one reservation and record the cloud calls made before it.

        Args:
            budget: The budget runtime.
            request: The priced create fields.
            operation_key: The sandbox identity.

        Returns:
            The fake reservation.
        """
        usage = FakeUsage(budget, request, operation_key, list(CLOUD.calls), settle_error=self.settle_error)
        self.made.append(usage)
        return usage


def _protected(recorder: UsageRecorder, **kwargs: Any) -> PrimeEnvironment:
    """Return a protected sandbox that reserves through the recorder.

    Args:
        recorder: The fake usage factory.
        **kwargs: Extra provider options.

    Returns:
        The sandbox.
    """
    return PrimeEnvironment(_spec(operation_key="op-1"), budget=object(), usage_factory=recorder, **kwargs)


def test_prime_is_billable_and_has_a_usage_adapter() -> None:
    """Declare Prime billable, so the broker passes a budget, and find its usage adapter."""
    assert prime.PRIME_CAPABILITIES.billable is True
    assert usage_adapter("prime") is not None


def test_a_protected_sandbox_needs_an_operation_key() -> None:
    """Refuse a budget without the stable identity the ledger needs."""
    with pytest.raises(SandboxSpecError, match="operation key"):
        PrimeEnvironment(_spec(), budget=object(), usage_factory=UsageRecorder())


async def test_a_protected_sandbox_reserves_before_the_create_and_settles_after_the_delete(
    fake_sdk: FakeCloud,
) -> None:
    """Reserve the funded size, confirm the created sandbox and settle its ended record."""
    recorder = UsageRecorder()
    env = _protected(recorder)
    assert env.capabilities.protected is True
    async with env:
        pass
    (usage,) = recorder.made
    assert "create" not in usage.created_before
    assert usage.operation_key == "op-1"
    assert usage.request == {**_SDK_DEFAULTS, "cpu_cores": 1.0, "disk_size_gb": 5.0, "timeout_minutes": 5}
    assert [name for name, _ in usage.calls] == ["confirm", "settle"]
    record = usage.calls[1][1]
    assert record["id"] == "sbx-1"
    assert record["created_at"] == _CREATED_AT.isoformat()
    assert record["terminated_at"] == (_CREATED_AT + timedelta(seconds=90)).isoformat()
    assert fake_sdk.calls.index("delete:sbx-1") < fake_sdk.calls.index("get:sbx-1")
    assert fake_sdk.calls[-1] == "aclose"


async def test_a_start_timeout_settles_the_created_sandbox(fake_sdk: FakeCloud) -> None:
    """Settle the sandbox that never reached running, and keep the start error."""
    fake_sdk.wait_hangs = True
    recorder = UsageRecorder()
    with pytest.raises(StartTimeoutError):
        await _protected(recorder, timeouts=_timeouts(start=0.05)).start()
    assert [name for name, _ in recorder.made[0].calls] == ["confirm", "settle"]


async def test_a_cancelled_create_settles_the_lost_sandbox(fake_sdk: FakeCloud) -> None:
    """Find a sandbox whose create never returned, bind it and settle it."""
    fake_sdk.create_hangs = True
    recorder = UsageRecorder()
    start = asyncio.ensure_future(_protected(recorder).start())
    await asyncio.sleep(0.01)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    (usage,) = recorder.made
    assert [name for name, _ in usage.calls] == ["confirm", "settle"]
    assert usage.calls[1][1]["status"] == "TERMINATED"


async def test_a_refused_create_releases_the_hold(fake_sdk: FakeCloud) -> None:
    """Release the hold when Prime refused the create and no sandbox carries the label."""
    fake_sdk.create_error = FakeAPIError("HTTP 422: bad field")
    recorder = UsageRecorder()
    with pytest.raises(SandboxRejectedError):
        await _protected(recorder).start()
    assert recorder.made[0].calls == [("release", "SandboxRejectedError")]


async def test_a_transient_create_error_keeps_the_hold_pending(fake_sdk: FakeCloud) -> None:
    """Keep the hold when a 5xx answer and an empty search leave it unknown whether Prime made a sandbox."""
    fake_sdk.create_error = FakeAPIError("HTTP 503: unavailable")
    recorder = UsageRecorder()
    with pytest.raises(SandboxTransientError):
        await _protected(recorder).start()
    assert recorder.made[0].calls == [("pending", None)]


async def test_a_failed_settle_is_raised_once_by_stop(fake_sdk: FakeCloud) -> None:
    """Raise the settle failure from the first stop only, and keep the hold pending."""
    recorder = UsageRecorder(settle_error=UsagePendingError("not final"))
    env = _protected(recorder)
    await env.start()
    with pytest.raises(UsagePendingError, match="not final"):
        await env.stop()
    await env.stop()
    assert [name for name, _ in recorder.made[0].calls] == ["confirm", "settle", "pending"]


@pytest.mark.parametrize(
    "resources",
    [ResourceRequest(cpus=1), ResourceRequest(cpus=4, memory_mb=8192, mode=ResourceMode.IGNORE)],
    ids=["no-disk", "ignore"],
)
async def test_a_protected_sandbox_with_no_disk_size_sends_and_quotes_the_default_disk(
    fake_sdk: FakeCloud, resources: ResourceRequest
) -> None:
    """Send the SDK's default disk in the create, and price that same disk in the quote.

    Args:
        resources: A request with no disk size, or one that ignores sizes.
    """
    recorder = UsageRecorder()
    env = PrimeEnvironment(_spec(operation_key="op-1", resources=resources), budget=object(), usage_factory=recorder)
    async with env:
        pass
    (usage,) = recorder.made
    assert usage.request["disk_size_gb"] == prime.DEFAULT_DISK_GB
    assert fake_sdk.boxes["sbx-1"].request["disk_size_gb"] == prime.DEFAULT_DISK_GB
    assert quote_prime_sandbox(usage.request).maximum.total > 0


async def test_a_stop_after_a_refused_create_but_before_start_handles_it_releases(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release the hold when a stop runs between a 4xx refusal and the start's own teardown."""
    recorder = UsageRecorder()
    env = _protected(recorder)
    stops: list[asyncio.Future[None]] = []

    async def refuse(self: FakeClient, request: FakeCreateRequest) -> SimpleNamespace:
        """Start a stop, then refuse the create, so the stop's teardown starts before start() sees the error.

        Args:
            request: The create request.

        Raises:
            FakeAPIError: Always, with HTTP 422.
        """
        stops.append(asyncio.ensure_future(env.stop()))
        raise FakeAPIError("HTTP 422: bad field")

    monkeypatch.setattr(FakeClient, "create", refuse)
    with pytest.raises(SandboxRejectedError):
        await env.start()
    await stops[0]
    assert recorder.made[0].calls == [("release", "SandboxRejectedError")]


async def test_a_sandbox_with_no_end_time_settles_until_the_delete_answer(fake_sdk: FakeCloud) -> None:
    """Charge the local time from the create to the accepted delete when Prime shows no end time."""
    fake_sdk.run_seconds = None
    recorder = UsageRecorder()
    async with _protected(recorder):
        pass
    (usage,) = recorder.made
    assert [name for name, _ in usage.calls] == ["confirm", "settle"]
    assert usage.calls[-1][1]["terminated_at"] is None
    (seconds,) = usage.seconds
    assert 0 <= seconds <= 5
    assert fake_sdk.calls.count("get:sbx-1") > 1


async def test_a_sandbox_whose_reads_fail_settles_until_the_delete_answer(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Charge the local time to the accepted delete, not the whole lifetime, when every read gets a 5xx."""

    async def unavailable(self: FakeClient, sandbox_id: str) -> SimpleNamespace:
        """Fail the read with a 5xx answer.

        Args:
            sandbox_id: The sandbox id.

        Raises:
            FakeAPIError: Always, with HTTP 503.
        """
        raise FakeAPIError("HTTP 503: unavailable")

    monkeypatch.setattr(FakeClient, "get", unavailable)
    recorder = UsageRecorder()
    async with _protected(recorder):
        pass
    (usage,) = recorder.made
    assert [name for name, _ in usage.calls] == ["confirm", "settle"]
    (seconds,) = usage.seconds
    assert 0 <= seconds <= 5


async def test_a_sandbox_with_no_end_and_no_delete_settles_at_its_whole_lifetime(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settle the whole funded timeout when no delete succeeded and Prime shows no end, since Prime ends the box then."""

    async def unavailable(self: FakeClient, sandbox_id: str) -> None:
        """Fail the delete with a 5xx answer.

        Args:
            sandbox_id: The sandbox id.

        Raises:
            FakeAPIError: Always, with HTTP 503.
        """
        raise FakeAPIError("HTTP 503: unavailable")

    monkeypatch.setattr(FakeClient, "delete", unavailable)
    recorder = UsageRecorder()
    async with _protected(recorder):
        pass
    (usage,) = recorder.made
    assert [name for name, _ in usage.calls] == ["confirm", "settle"]
    assert usage.seconds == [5 * 60]


async def test_a_failed_delete_is_tried_again(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """Try the delete again after a 5xx answer, then settle the ended record."""
    original = FakeClient.delete
    tries = 0

    async def flaky(self: FakeClient, sandbox_id: str) -> None:
        """Fail the first delete with a 5xx answer.

        Args:
            sandbox_id: The sandbox id.

        Raises:
            FakeAPIError: On the first try.
        """
        nonlocal tries
        tries += 1
        if tries == 1:
            raise FakeAPIError("HTTP 503: unavailable")
        await original(self, sandbox_id)

    monkeypatch.setattr(FakeClient, "delete", flaky)
    recorder = UsageRecorder()
    async with _protected(recorder):
        pass
    (usage,) = recorder.made
    assert tries == 2
    assert usage.calls[-1][1]["terminated_at"] is not None
    assert usage.seconds == [None]


async def test_a_sandbox_that_prime_no_longer_knows_settles_on_local_time(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settle on the local time since the create when the read after the delete gets a 404."""

    async def gone(self: FakeClient, sandbox_id: str) -> SimpleNamespace:
        """Answer that the sandbox does not exist.

        Args:
            sandbox_id: The sandbox id.

        Raises:
            FakeAPIError: Always, with HTTP 404.
        """
        raise FakeAPIError("HTTP 404: not found")

    monkeypatch.setattr(FakeClient, "get", gone)
    recorder = UsageRecorder()
    async with _protected(recorder):
        pass
    (usage,) = recorder.made
    assert [name for name, _ in usage.calls] == ["confirm", "settle"]
    assert usage.calls[-1][1] == {}
    (seconds,) = usage.seconds
    assert isinstance(seconds, int)
    assert 0 <= seconds <= 5


@pytest.mark.parametrize("error", [FakeUnauthorizedError("no key"), FakePaymentRequiredError("no funds")])
async def test_a_create_refused_with_401_or_402_stays_pending_when_the_search_fails(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """Keep the hold when the label search timed out: an SDK retry can get 401 or 402 after a first try made a box.

    Args:
        error: The SDK refusal.
    """
    monkeypatch.setattr(prime, "STOP_TIMEOUT", 0.05)
    fake_sdk.create_error = error
    fake_sdk.list_hangs = True
    recorder = UsageRecorder()
    with pytest.raises(SandboxFatalError):
        await _protected(recorder).start()
    assert recorder.made[0].calls == [("pending", None)]


async def test_a_create_refused_with_402_and_no_box_releases(fake_sdk: FakeCloud) -> None:
    """Release the hold when Prime refused the create with 402 and the label search found nothing."""
    fake_sdk.create_error = FakePaymentRequiredError("no funds")
    recorder = UsageRecorder()
    with pytest.raises(SandboxFatalError):
        await _protected(recorder).start()
    assert recorder.made[0].calls == [("release", "SandboxFatalError")]


def _create_then_refuse(monkeypatch: pytest.MonkeyPatch, *, ended: bool) -> list[BoxState]:
    """Make each create build a box and then answer 402, like an SDK retry after a first try.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        ended: End the box before the answer.

    Returns:
        The boxes made, in order.
    """
    original = FakeClient.create
    made: list[BoxState] = []

    async def create(self: FakeClient, request: FakeCreateRequest) -> SimpleNamespace:
        """Make the box, then refuse the create.

        Args:
            request: The create request.

        Raises:
            FakePaymentRequiredError: Always, after the box exists.
        """
        view = await original(self, request)
        box = CLOUD.boxes[view.id]
        if ended:
            box.status = "TERMINATED"
            box.terminated_at = _CREATED_AT + timedelta(seconds=30)
        made.append(box)
        raise FakePaymentRequiredError("no funds")

    monkeypatch.setattr(FakeClient, "create", create)
    return made


async def test_a_refused_create_whose_box_already_ended_settles(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Find an ended box under the label, settle its record and never delete or release it."""
    _create_then_refuse(monkeypatch, ended=True)
    recorder = UsageRecorder()
    with pytest.raises(SandboxFatalError):
        await _protected(recorder).start()
    (usage,) = recorder.made
    assert not any(call.startswith("delete:") for call in fake_sdk.calls)
    assert [name for name, _ in usage.calls] == ["confirm", "settle"]
    assert usage.calls[-1][1]["terminated_at"] == (_CREATED_AT + timedelta(seconds=30)).isoformat()
    assert usage.seconds == [None]


async def test_a_refused_create_whose_box_still_runs_settles(fake_sdk: FakeCloud) -> None:
    """Find a running box under the label after a refused create, delete it and settle it, never release it."""
    original = FakeClient.create

    async def create(self: FakeClient, request: FakeCreateRequest) -> SimpleNamespace:
        """Make a running box, then answer 400 like an SDK retry after a 5xx.

        Args:
            request: The create request.

        Raises:
            FakeAPIError: Always, with HTTP 400, after the box exists.
        """
        view = await original(self, request)
        CLOUD.boxes[view.id].status = "RUNNING"
        raise FakeAPIError("HTTP 400: duplicate")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeClient, "create", create)
        recorder = UsageRecorder()
        with pytest.raises(SandboxRejectedError):
            await _protected(recorder).start()
    (usage,) = recorder.made
    assert fake_sdk.list_calls[0]["exclude_terminated"] is None
    assert "delete:sbx-1" in fake_sdk.calls
    assert [name for name, _ in usage.calls] == ["confirm", "settle"]
    assert usage.seconds == [None]


async def test_a_refused_create_whose_box_is_listed_late_settles(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Search again after a refused create, and delete and settle the box that the second search finds."""
    made = _create_then_refuse(monkeypatch, ended=False)
    original = FakeClient.list
    hidden: dict[str, BoxState] = {}

    async def late(self: FakeClient, **kwargs: Any) -> SimpleNamespace:
        """Hide the new box from the first search only.

        Args:
            **kwargs: The list options.

        Returns:
            An object like ``SandboxListResponse``.
        """
        if not fake_sdk.list_calls:
            hidden.update({box.id: fake_sdk.boxes.pop(box.id) for box in made})
        else:
            fake_sdk.boxes.update(hidden)
        return await original(self, **kwargs)

    monkeypatch.setattr(FakeClient, "list", late)
    recorder = UsageRecorder()
    with pytest.raises(SandboxFatalError):
        await _protected(recorder).start()
    (usage,) = recorder.made
    assert len(fake_sdk.list_calls) == 2
    assert "delete:sbx-1" in fake_sdk.calls
    assert [name for name, _ in usage.calls] == ["confirm", "settle"]
    assert usage.seconds == [None]


async def test_a_create_conflict_stays_pending(fake_sdk: FakeCloud) -> None:
    """Keep the hold after a 409, since a retried create may have made a box that Prime does not list yet."""
    fake_sdk.create_error = FakeAPIError("HTTP 409: conflict")
    recorder = UsageRecorder()
    with pytest.raises(SandboxRejectedError):
        await _protected(recorder).start()
    assert recorder.made[0].calls == [("pending", None)]


async def test_a_lost_sandbox_whose_delete_fails_still_settles(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the id of a found lost sandbox after a failed delete, and settle its whole funded timeout."""

    async def failing(self: FakeClient, sandbox_id: str) -> None:
        """Fail every delete.

        Args:
            sandbox_id: The sandbox id.

        Raises:
            FakeAPIError: Always, with a 5xx answer.
        """
        CLOUD.calls.append(f"delete:{sandbox_id}")
        raise FakeAPIError("HTTP 503: unavailable")

    monkeypatch.setattr(prime, "STOP_TIMEOUT", 0.1)
    monkeypatch.setattr(FakeClient, "delete", failing)
    fake_sdk.create_hangs = True
    recorder = UsageRecorder()
    start = asyncio.ensure_future(_protected(recorder).start())
    await asyncio.sleep(0.01)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    (usage,) = recorder.made
    assert fake_sdk.calls.count("delete:sbx-1") > 1
    assert [name for name, _ in usage.calls] == ["confirm", "settle"]
    assert usage.sandbox_id == "sbx-1"
    assert usage.seconds == [5 * 60]


async def test_the_read_waits_for_an_ended_status(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read again while Prime shows an end time but not yet an ended status."""
    original = FakeClient.get
    reads = 0

    async def ending(self: FakeClient, sandbox_id: str) -> SimpleNamespace:
        """Show an end time with a status that has not ended on the first read.

        Args:
            sandbox_id: The sandbox id.

        Returns:
            An object like ``Sandbox``.
        """
        nonlocal reads
        reads += 1
        view = await original(self, sandbox_id)
        if reads == 1:
            view.status = "STOPPING"
        return view

    monkeypatch.setattr(FakeClient, "get", ending)
    recorder = UsageRecorder()
    async with _protected(recorder):
        pass
    (usage,) = recorder.made
    assert reads == 2
    assert usage.calls[-1][1]["status"] == "TERMINATED"
    assert usage.seconds == [None]
