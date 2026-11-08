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
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import settings
from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import (
    RunTimeoutError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
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


class FakeCreateRequest(dict):
    """Stand in for ``CreateSandboxRequest``: keep the fields as a dict."""

    def __init__(self, **fields: Any) -> None:
        """Keep the request fields.

        Args:
            **fields: The request fields.
        """
        super().__init__(fields)


@dataclass
class BoxState:
    """Hold one fake sandbox in the fake cloud."""

    id: str
    labels: list[str]
    request: dict[str, Any]
    status: str = "PENDING"
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
        return SimpleNamespace(id=box.id)

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
        self, *, labels: list[str], exclude_terminated: bool, per_page: int, cursor: str | None
    ) -> SimpleNamespace:
        """Return one page of sandboxes with these labels.

        Args:
            labels: The label filter.
            exclude_terminated: Leave terminated sandboxes out.
            per_page: The page size the provider asked for.
            cursor: The page cursor.

        Returns:
            An object like ``SandboxListResponse``.
        """
        CLOUD.list_calls.append({"labels": labels, "per_page": per_page, "cursor": cursor})
        boxes = [
            box
            for box in CLOUD.boxes.values()
            if (CLOUD.list_ignores_labels or set(labels) <= set(box.labels))
            and not (exclude_terminated and box.status == "TERMINATED")
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
        CLOUD.boxes[sandbox_id].status = "TERMINATED"

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


def test_the_default_sandbox_backend_stays_legacy() -> None:
    """Keep the legacy backend as the default."""
    assert type(settings).model_fields["sandbox_backend"].default == "legacy"


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
    assert fields["docker_image"] == prime.DEFAULT_IMAGE
    assert fields["name"] == f"skynet-{env._box_tag[:12]}"
    assert fields["timeout_minutes"] == 1
    assert "secrets" not in fields
    assert "memory_gb" not in fields
    assert "disk_size_gb" not in fields
    assert len(_fields(_spec(name="x" * 300))["name"]) == prime._NAME_LIMIT


def test_ignore_mode_sends_no_size() -> None:
    """Send no size when the spec ignores resources."""
    fields = _fields(_spec(resources=ResourceRequest(cpus=64, memory_mb=1024, mode=ResourceMode.IGNORE)))
    assert not {"cpu_cores", "memory_gb", "disk_size_gb"} & fields.keys()


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


async def test_a_timed_out_command_raises_run_timeout(fake_sdk: FakeCloud) -> None:
    """Raise RunTimeoutError at the local time limit."""
    async with PrimeEnvironment(_spec()) as env:
        fake_sdk.exec_hangs = True
        with pytest.raises(RunTimeoutError):
            await env.exec("true", timeout_sec=0.05)
    assert fake_sdk.exec_calls[-1]["timeout"] == 1


async def test_the_run_timeout_is_the_default_limit(fake_sdk: FakeCloud) -> None:
    """Use the run phase timeout when the call sets no limit."""
    async with PrimeEnvironment(_spec(), timeouts=_timeouts(run=0.05)) as env:
        fake_sdk.exec_hangs = True
        with pytest.raises(RunTimeoutError):
            await env.exec("true")


async def test_an_sdk_command_timeout_raises_run_timeout(fake_sdk: FakeCloud) -> None:
    """Turn the SDK command timeout into RunTimeoutError."""
    async with PrimeEnvironment(_spec()) as env:
        fake_sdk.exec_error = FakeCommandTimeoutError("timed out")
        with pytest.raises(RunTimeoutError):
            await env.exec("true", timeout_sec=5)


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
