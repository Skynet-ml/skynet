"""Tests for the Runloop devbox provider, against an in-memory fake of the SDK.

No test reaches Runloop: an autouse fixture swaps the SDK client, its error
classes and its polling config for fakes, so the suite needs no network, no
API key and no ``runloop`` extra.
"""

from __future__ import annotations

import asyncio
import itertools
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest

from core.config import settings
from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG
from core.sandbox.errors import (
    RunTimeoutError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from core.sandbox.providers import runloop
from core.sandbox.providers.runloop import BOX_TAG, RunloopEnvironment
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.timeouts import PhaseTimeouts

from .test_environment import SandboxEnvironmentContract


class FakeRunloopError(Exception):
    """Stand in for ``runloop_api_client.RunloopError``."""


class FakeAPIStatusError(FakeRunloopError):
    """Stand in for ``APIStatusError``."""


class FakeNotFoundError(FakeAPIStatusError):
    """Stand in for ``NotFoundError``."""


class FakeConflictError(FakeAPIStatusError):
    """Stand in for ``ConflictError``."""


class FakeBadRequestError(FakeAPIStatusError):
    """Stand in for ``BadRequestError``."""


class FakeUnprocessableEntityError(FakeAPIStatusError):
    """Stand in for ``UnprocessableEntityError``."""


class FakeAuthenticationError(FakeAPIStatusError):
    """Stand in for ``AuthenticationError``."""


class FakePermissionDeniedError(FakeAPIStatusError):
    """Stand in for ``PermissionDeniedError``."""


class FakeRateLimitError(FakeAPIStatusError):
    """Stand in for ``RateLimitError``."""


class FakeInternalServerError(FakeAPIStatusError):
    """Stand in for ``InternalServerError``."""


class FakeAPIConnectionError(FakeRunloopError):
    """Stand in for ``APIConnectionError``."""


@dataclass
class FakePollingConfig:
    """Stand in for ``runloop_api_client.lib.polling.PollingConfig``."""

    interval_seconds: float = 1.0
    max_attempts: int = 120
    timeout_seconds: float | None = None


@dataclass
class BoxState:
    """Hold one fake devbox in the fake cloud."""

    id: str
    name: str
    metadata: dict[str, str]
    env: dict[str, str]
    launch: dict[str, Any]
    status: str = "provisioning"
    files: dict[str, str] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=lambda: {"/", "/tmp"})


@dataclass
class FakeCloud:
    """Hold the shared state of the fake Runloop account."""

    boxes: dict[str, BoxState] = field(default_factory=dict)
    policies: list[SimpleNamespace] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    clients: list[dict[str, Any]] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    list_limits: list[int] = field(default_factory=list)
    list_hangs: bool = False
    create_error: BaseException | None = None
    create_hangs: bool = False
    exec_hangs: bool = False
    policy_conflict: SimpleNamespace | None = None
    ids: Any = field(default_factory=lambda: itertools.count(1))

    def run(self, box: BoxState, command: str) -> tuple[int, str, str]:
        """Run the provider's wrapper line in a tiny shell model.

        Args:
            box: The devbox.
            command: The full wrapper line that ``exec_async`` received.

        Returns:
            The exit code, stdout and stderr.
        """
        tokens = shlex.split(command)
        if tokens[0] == "cd":
            tokens = tokens[3:]
        env = dict(box.env)
        while "=" in tokens[0] and tokens[0] != "bash":
            key, _, value = tokens.pop(0).partition("=")
            env[key] = value
        assert tokens[:2] == ["bash", "-c"]
        words = shlex.split(tokens[2])
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
            return (0, env[args[0]] + "\n", "") if args[0] in env else (1, "", "")
        if verb == "cat":
            return (0, box.files[args[0]], "") if args[0] in box.files else (1, "", "No such file\n")
        if verb == "mkdir" and args[0] == "-p":
            for path in args[1:]:
                parts = path.strip("/").split("/")
                box.dirs.update("/" + "/".join(parts[: n + 1]) for n in range(len(parts)))
            return 0, "", ""
        return 127, "", f"{verb}: command not found\n"


CLOUD = FakeCloud()


class FakeExecution:
    """Stand in for ``AsyncExecution``."""

    def __init__(self, execution_id: str, devbox_id: str, outcome: tuple[int, str, str]) -> None:
        """Keep the outcome of the command.

        Args:
            execution_id: The execution id.
            devbox_id: The devbox id.
            outcome: The exit code, stdout and stderr.
        """
        self.execution_id = execution_id
        self.devbox_id = devbox_id
        self._outcome = outcome

    async def result(self) -> SimpleNamespace:
        """Return the finished result.

        Returns:
            An object like ``AsyncExecutionResult``.
        """
        code, out, err = self._outcome

        async def stdout() -> str:
            """Return the captured stdout."""
            return out

        async def stderr() -> str:
            """Return the captured stderr."""
            return err

        return SimpleNamespace(exit_code=code, stdout=stdout, stderr=stderr)

    async def kill(self) -> None:
        """Record that the command was killed."""
        CLOUD.killed.append(self.execution_id)


class FakeDevbox:
    """Stand in for ``AsyncDevbox``."""

    def __init__(self, box: BoxState) -> None:
        """Wrap one fake box.

        Args:
            box: The box state.
        """
        self.id = box.id
        self._box = box
        self.cmd = SimpleNamespace(exec_async=self._exec_async)
        self.file = SimpleNamespace(write=self._write, download=self._download)

    async def _exec_async(self, command: str, stdout: Any = None, stderr: Any = None) -> FakeExecution:
        """Run the command and stream its lines.

        Args:
            command: The wrapper line.
            stdout: The stdout line callback.
            stderr: The stderr line callback.

        Returns:
            The execution handle.
        """
        CLOUD.calls.append(f"exec:{command}")
        outcome = CLOUD.run(self._box, command)
        for callback, text in ((stdout, outcome[1]), (stderr, outcome[2])):
            if callback is not None:
                for line in text.splitlines(keepends=True):
                    callback(line)
        return FakeExecution(f"exec-{next(CLOUD.ids)}", self.id, outcome)

    async def _write(self, *, file_path: str, contents: str) -> None:
        """Write a file whose parent directory exists.

        Args:
            file_path: The absolute path.
            contents: The text.

        Raises:
            FakeBadRequestError: The parent directory does not exist.
        """
        if str(PurePosixPath(file_path).parent) not in self._box.dirs:
            raise FakeBadRequestError("no parent directory")
        self._box.files[file_path] = contents

    async def _download(self, *, path: str) -> bytes:
        """Return the file bytes.

        Args:
            path: The absolute path.

        Returns:
            The UTF-8 bytes.

        Raises:
            FakeNotFoundError: The file does not exist.
        """
        if path not in self._box.files:
            raise FakeNotFoundError(path)
        return self._box.files[path].encode("utf-8")

    async def shutdown(self) -> None:
        """Shut the box down."""
        CLOUD.calls.append(f"shutdown:{self.id}")
        self._box.status = "shutdown"


class FakeSDK:
    """Stand in for ``AsyncRunloopSDK``."""

    def __init__(self, **kwargs: Any) -> None:
        """Record the client options.

        Args:
            **kwargs: The SDK constructor options.
        """
        CLOUD.clients.append(kwargs)
        self.devbox = SimpleNamespace(create=self._create)
        self.api = SimpleNamespace(
            devboxes=SimpleNamespace(
                list=self._list_boxes,
                shutdown=self._shutdown,
                executions=SimpleNamespace(await_completed=self._await_completed),
            ),
            network_policies=SimpleNamespace(list=self._list_policies, create=self._create_policy),
        )

    async def _create(self, **params: Any) -> FakeDevbox:
        """Create a box and wait until it runs.

        Args:
            **params: The create parameters.

        Returns:
            The running devbox.
        """
        CLOUD.calls.append("create")
        CLOUD.clients[-1]["create"] = params
        if CLOUD.create_error is not None:
            raise CLOUD.create_error
        box = BoxState(
            id=f"dbx-{next(CLOUD.ids)}",
            name=params["name"],
            metadata=dict(params["metadata"]),
            env=dict(params["environment_variables"]),
            launch=dict(params["launch_parameters"]),
        )
        CLOUD.boxes[box.id] = box
        if CLOUD.create_hangs:
            await asyncio.Event().wait()
        box.status = "running"
        return FakeDevbox(box)

    async def _list_boxes(self, *, limit: int) -> AsyncIterator[SimpleNamespace]:
        """Yield every box, like the SDK's async paginator.

        Args:
            limit: The page size.

        Yields:
            Box views. The listing hangs after the last box when the cloud
            says it hangs.
        """
        CLOUD.list_limits.append(limit)
        for box in list(CLOUD.boxes.values()):
            yield SimpleNamespace(id=box.id, metadata=box.metadata, status=box.status)
        if CLOUD.list_hangs:
            await asyncio.Event().wait()

    async def _shutdown(self, box_id: str) -> None:
        """Shut a box down by id.

        Args:
            box_id: The devbox id.
        """
        CLOUD.calls.append(f"shutdown:{box_id}")
        CLOUD.boxes[box_id].status = "shutdown"

    async def _await_completed(self, execution_id: str, *, devbox_id: str, polling_config: Any) -> None:
        """Wait for the command, forever when the cloud says it hangs.

        Args:
            execution_id: The execution id.
            devbox_id: The devbox id.
            polling_config: The SDK polling config.
        """
        assert polling_config.max_attempts == runloop._UNBOUNDED_POLLS
        if CLOUD.exec_hangs:
            await asyncio.Event().wait()

    async def _list_policies(self, *, name: str, limit: int) -> AsyncIterator[SimpleNamespace]:
        """Yield the policies with this name.

        Args:
            name: The policy name.
            limit: The page size.

        Yields:
            Policy views.
        """
        for policy in list(CLOUD.policies):
            if policy.name == name:
                yield policy

    async def _create_policy(self, **params: Any) -> SimpleNamespace:
        """Create a network policy, or raise a conflict once.

        Args:
            **params: The create parameters.

        Returns:
            The policy view.

        Raises:
            FakeConflictError: Another caller created the same policy first.
        """
        CLOUD.calls.append(f"policy:{params['name']}")
        if CLOUD.policy_conflict is not None:
            CLOUD.policies.append(CLOUD.policy_conflict)
            CLOUD.policy_conflict = None
            raise FakeConflictError(params["name"])
        egress = SimpleNamespace(
            allow_all=params["allow_all"],
            allow_agent_gateway=params["allow_agent_gateway"],
            allow_devbox_to_devbox=params["allow_devbox_to_devbox"],
            allow_mcp_gateway=params["allow_mcp_gateway"],
            allow_runloop_mirrors=params["allow_runloop_mirrors"],
            allowed_cidrs=[],
            allowed_hostnames=list(params["allowed_hostnames"]),
        )
        policy = SimpleNamespace(id=f"np-{next(CLOUD.ids)}", name=params["name"], egress=egress)
        CLOUD.policies.append(policy)
        return policy

    async def aclose(self) -> None:
        """Close the client."""
        CLOUD.calls.append("aclose")


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> FakeCloud:
    """Swap the Runloop SDK for the fake and reset the fake cloud.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fresh fake cloud.
    """
    global CLOUD
    CLOUD = FakeCloud()
    fakes = {
        "AsyncRunloopSDK": FakeSDK,
        "PollingConfig": FakePollingConfig,
        "RunloopError": FakeRunloopError,
        "APIStatusError": FakeAPIStatusError,
        "NotFoundError": FakeNotFoundError,
        "ConflictError": FakeConflictError,
        "BadRequestError": FakeBadRequestError,
        "UnprocessableEntityError": FakeUnprocessableEntityError,
        "AuthenticationError": FakeAuthenticationError,
        "PermissionDeniedError": FakePermissionDeniedError,
        "RateLimitError": FakeRateLimitError,
        "InternalServerError": FakeInternalServerError,
        "APIConnectionError": FakeAPIConnectionError,
    }
    for name, value in fakes.items():
        monkeypatch.setattr(runloop, name, value)
    monkeypatch.setenv(runloop.API_KEY_ENV, "fake-key-for-tests")
    monkeypatch.setattr(runloop, "STOP_TIMEOUT", 1.0)
    monkeypatch.setattr(runloop, "KILL_TIMEOUT", 1.0)
    return CLOUD


class TestRunloopEnvironmentContract(SandboxEnvironmentContract):
    """Run the shared provider contract against the fake Runloop cloud."""

    provider_cls = RunloopEnvironment


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


def _launch(spec: SandboxSpec) -> dict[str, Any]:
    """Return the launch parameters that the provider builds for a spec.

    Args:
        spec: The spec.

    Returns:
        The launch parameters.
    """
    return RunloopEnvironment(spec)._create_params["launch_parameters"]


def test_runloop_is_not_allowed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the Runloop provider in production before its import (D1)."""
    assert factory.PROVIDERS["runloop"].production_allowed is False
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(factory.ProductionNotAllowedError, match="runloop"):
        factory.load_provider("runloop")


def test_factory_loads_runloop_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the Runloop class from the registry outside production."""
    monkeypatch.setattr(factory.settings, "app_environment", "staging")
    assert factory.load_provider("runloop") is RunloopEnvironment


def test_the_default_sandbox_backend_stays_legacy() -> None:
    """Keep the legacy backend as the default."""
    assert type(settings).model_fields["sandbox_backend"].default == "legacy"


def test_preflight_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the extra when the SDK is not installed."""
    monkeypatch.setattr(runloop, "AsyncRunloopSDK", None)
    with pytest.raises(SandboxFatalError, match="`runloop` extra"):
        RunloopEnvironment.preflight()


def test_preflight_needs_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the variable when the API key is not set."""
    monkeypatch.delenv(runloop.API_KEY_ENV)
    with pytest.raises(SandboxFatalError, match=runloop.API_KEY_ENV):
        RunloopEnvironment.preflight()


async def test_the_client_never_retries(fake_sdk: FakeCloud) -> None:
    """Turn the SDK retries off (D3, D10)."""
    async with RunloopEnvironment(_spec()):
        pass
    assert fake_sdk.clients[0]["max_retries"] == 0


async def test_a_transient_create_error_is_raised_once(fake_sdk: FakeCloud) -> None:
    """Raise a transient error after one create call, with no retry (D3, D10)."""
    fake_sdk.create_error = FakeRateLimitError("slow down")
    with pytest.raises(SandboxTransientError):
        async with RunloopEnvironment(_spec()):
            pass
    assert fake_sdk.calls.count("create") == 1
    assert fake_sdk.calls[-1] == "aclose"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeAPIConnectionError("down"), SandboxTransientError),
        (FakeInternalServerError("500"), SandboxTransientError),
        (FakeAuthenticationError("401"), SandboxFatalError),
        (FakePermissionDeniedError("403"), SandboxFatalError),
        (FakeBadRequestError("400"), SandboxRejectedError),
        (FakeUnprocessableEntityError("422"), SandboxRejectedError),
        (FakeRunloopError("failed"), SandboxInfraError),
    ],
)
async def test_sdk_errors_map_to_sandbox_errors(
    fake_sdk: FakeCloud, error: Exception, expected: type[Exception]
) -> None:
    """Map each SDK error group to its sandbox error and keep the cause."""
    fake_sdk.create_error = error
    with pytest.raises(expected) as caught:
        await RunloopEnvironment(_spec()).start()
    assert caught.value.__cause__ is error


async def test_create_sends_the_tags_env_and_idempotency_key(fake_sdk: FakeCloud) -> None:
    """Tag the box, pass the spec env and use the box tag as the idempotency key."""
    env = RunloopEnvironment(_spec(tags={JOB_TAG: "job-1"}, env={"A": "1"}))
    async with env:
        params = fake_sdk.clients[0]["create"]
    assert params["metadata"] == {JOB_TAG: "job-1", BOX_TAG: env._box_tag}
    assert params["environment_variables"] == {"A": "1"}
    assert params["idempotency_key"] == env._box_tag
    assert params["polling_config"].max_attempts == runloop._UNBOUNDED_POLLS


def test_sizes_map_to_a_custom_runloop_size() -> None:
    """Round memory and disk up to even GiB and keep at least 2 GiB per vCPU."""
    launch = _launch(_spec(resources=ResourceRequest(cpus=2, memory_mb=5000, disk_mb=3000)))
    assert launch["resource_size_request"] == "CUSTOM_SIZE"
    assert (launch["custom_cpu_cores"], launch["custom_gb_memory"], launch["custom_disk_size"]) == (2, 6, 4)
    assert _launch(_spec(resources=ResourceRequest(cpus=4)))["custom_gb_memory"] == 8
    assert "custom_disk_size" not in _launch(_spec())


def test_launch_runs_as_root_with_a_capped_keep_alive() -> None:
    """Run as root and cap the keep-alive at the Runloop maximum."""
    launch = _launch(_spec(lifetime_seconds=10**6))
    assert launch["keep_alive_time_seconds"] == 172_800
    assert launch["user_parameters"] == {"uid": 0, "username": "root"}


def test_ignore_mode_sends_no_size() -> None:
    """Send no custom size when the spec ignores resources."""
    launch = _launch(_spec(resources=ResourceRequest(cpus=64, mode=ResourceMode.IGNORE)))
    assert "resource_size_request" not in launch


def test_rejects_memory_above_eight_gib_per_vcpu() -> None:
    """Reject a memory size that Runloop cannot give to the vCPUs."""
    with pytest.raises(SandboxSpecError, match="8 GiB"):
        RunloopEnvironment(_spec(resources=ResourceRequest(cpus=1, memory_mb=9 * 1024)))


def test_rejects_odd_vcpus_above_one() -> None:
    """Reject an odd vCPU count above one."""
    with pytest.raises(SandboxSpecError):
        RunloopEnvironment(_spec(resources=ResourceRequest(cpus=3)))


async def test_open_egress_sends_no_network_policy(fake_sdk: FakeCloud) -> None:
    """Leave the network policy out for open egress."""
    async with RunloopEnvironment(_spec()):
        pass
    assert "network_policy_id" not in fake_sdk.clients[0]["create"]["launch_parameters"]
    assert fake_sdk.policies == []


async def test_allowlist_creates_then_reuses_one_policy(fake_sdk: FakeCloud) -> None:
    """Create one allowlist policy and reuse it for a box with the same hosts."""
    async with RunloopEnvironment(_spec(allowed_hosts=("b.example", "a.example", "a.example"))):
        pass
    async with RunloopEnvironment(_spec(allowed_hosts=("a.example", "b.example"))):
        pass
    assert len(fake_sdk.policies) == 1
    policy = fake_sdk.policies[0]
    assert policy.egress.allowed_hostnames == ["a.example", "b.example"]
    assert [client["create"]["launch_parameters"]["network_policy_id"] for client in fake_sdk.clients] == [
        policy.id,
        policy.id,
    ]


async def test_disabled_network_uses_the_no_network_policy(fake_sdk: FakeCloud) -> None:
    """Use an empty allowlist policy for deny-all egress."""
    async with RunloopEnvironment(_spec(network_disabled=True)):
        pass
    (policy,) = fake_sdk.policies
    assert policy.name == "skynet-network-policy-no-network-v1"
    assert policy.egress.allowed_hostnames == []


async def test_policy_conflict_reads_the_policy_again(fake_sdk: FakeCloud) -> None:
    """Use the policy that a parallel create made when the create conflicts."""
    name = runloop._policy_name(["a.example"])
    egress = SimpleNamespace(
        allow_all=False,
        allow_agent_gateway=False,
        allow_devbox_to_devbox=False,
        allow_mcp_gateway=False,
        allowed_hostnames=["a.example"],
    )
    fake_sdk.policy_conflict = SimpleNamespace(id="np-parallel", name=name, egress=egress)
    async with RunloopEnvironment(_spec(allowed_hosts=("a.example",))):
        pass
    assert fake_sdk.clients[0]["create"]["launch_parameters"]["network_policy_id"] == "np-parallel"


async def test_policy_with_other_egress_is_fatal(fake_sdk: FakeCloud) -> None:
    """Refuse a policy with the same name that allows other egress."""
    name = runloop._policy_name(["a.example"])
    egress = SimpleNamespace(allow_all=True, allowed_hostnames=[])
    fake_sdk.policies.append(SimpleNamespace(id="np-open", name=name, egress=egress))
    with pytest.raises(SandboxFatalError, match="different egress"):
        await RunloopEnvironment(_spec(allowed_hosts=("a.example",))).start()
    assert "create" not in fake_sdk.calls


def _egress(**overrides: Any) -> SimpleNamespace:
    """Return a policy egress that allows only ``a.example``.

    Args:
        **overrides: Egress fields to replace.

    Returns:
        The egress view.
    """
    fields = {
        "allow_all": False,
        "allow_agent_gateway": False,
        "allow_devbox_to_devbox": False,
        "allow_mcp_gateway": False,
        "allow_runloop_mirrors": False,
        "allowed_cidrs": [],
        "allowed_hostnames": ["a.example"],
    }
    return SimpleNamespace(**{**fields, **overrides})


_WIDER_EGRESS = [
    pytest.param({"allow_runloop_mirrors": True}, id="mirrors"),
    pytest.param({"allowed_cidrs": [SimpleNamespace(cidr="0.0.0.0/0")]}, id="cidrs"),
]


async def test_policy_create_turns_the_mirrors_off(fake_sdk: FakeCloud) -> None:
    """Create the policy with the Runloop mirrors off and no CIDRs."""
    async with RunloopEnvironment(_spec(allowed_hosts=("a.example",))):
        pass
    (policy,) = fake_sdk.policies
    assert policy.egress.allow_runloop_mirrors is False
    assert policy.egress.allowed_cidrs == []


async def test_policy_with_reordered_or_upper_case_hosts_is_reused(fake_sdk: FakeCloud) -> None:
    """Reuse a same-name policy whose hosts differ only in order or case."""
    name = runloop._policy_name(["a.example", "b.example"])
    egress = _egress(allowed_hostnames=["B.Example", "a.example"])
    fake_sdk.policies.append(SimpleNamespace(id="np-same", name=name, egress=egress))
    async with RunloopEnvironment(_spec(allowed_hosts=("a.example", "b.example"))):
        pass
    assert f"policy:{name}" not in fake_sdk.calls
    assert [policy.id for policy in fake_sdk.policies] == ["np-same"]
    assert fake_sdk.clients[0]["create"]["launch_parameters"]["network_policy_id"] == "np-same"


@pytest.mark.parametrize("wider", _WIDER_EGRESS)
async def test_policy_with_mirrors_or_cidrs_is_fatal(fake_sdk: FakeCloud, wider: dict[str, Any]) -> None:
    """Refuse a same-name policy that also allows the mirrors or CIDRs."""
    name = runloop._policy_name(["a.example"])
    fake_sdk.policies.append(SimpleNamespace(id="np-wide", name=name, egress=_egress(**wider)))
    with pytest.raises(SandboxFatalError, match="different egress"):
        await RunloopEnvironment(_spec(allowed_hosts=("a.example",))).start()
    assert "create" not in fake_sdk.calls


@pytest.mark.parametrize("wider", _WIDER_EGRESS)
async def test_conflict_policy_with_mirrors_or_cidrs_is_fatal(fake_sdk: FakeCloud, wider: dict[str, Any]) -> None:
    """Refuse a parallel policy that also allows the mirrors or CIDRs."""
    name = runloop._policy_name(["a.example"])
    fake_sdk.policy_conflict = SimpleNamespace(id="np-wide", name=name, egress=_egress(**wider))
    with pytest.raises(SandboxFatalError, match="different egress"):
        await RunloopEnvironment(_spec(allowed_hosts=("a.example",))).start()
    assert "create" not in fake_sdk.calls


async def test_a_second_start_is_refused(fake_sdk: FakeCloud) -> None:
    """Refuse a second start, while the box runs and after the stop."""
    env = RunloopEnvironment(_spec())
    await env.start()
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()
    await env.stop()
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()
    assert fake_sdk.calls.count("create") == 1


async def test_a_stop_before_the_start_still_lets_the_box_stop(fake_sdk: FakeCloud) -> None:
    """Start after an early stop, then shut the box down at the next stop."""
    env = RunloopEnvironment(_spec())
    await env.stop()
    await env.start()
    await env.stop()
    (box,) = fake_sdk.boxes.values()
    assert box.status == "shutdown"


async def test_a_second_start_after_a_failed_start_is_refused(fake_sdk: FakeCloud) -> None:
    """Refuse a start after a failed start, so no create reuses the box tag."""
    fake_sdk.create_error = FakeRateLimitError("slow down")
    env = RunloopEnvironment(_spec())
    with pytest.raises(SandboxTransientError):
        await env.start()
    fake_sdk.create_error = None
    with pytest.raises(SandboxInfraError, match="cannot start again"):
        await env.start()
    assert fake_sdk.calls.count("create") == 1


async def test_exec_wraps_cwd_and_env() -> None:
    """Run in the cwd with quoted env values."""
    async with RunloopEnvironment(_spec()) as env:
        result = await env.exec("printenv X", cwd="/tmp", env={"X": "a b'c"})
    assert result.stdout == "a b'c\n"
    assert RunloopEnvironment._wrap("ls", "/w d", {"K": "v"}) == "cd '/w d' && K=v bash -c ls"


async def test_exec_rejects_a_bad_env_name() -> None:
    """Reject an env name that the shell cannot set."""
    async with RunloopEnvironment(_spec()) as env:
        with pytest.raises(SandboxRejectedError):
            await env.exec("true", env={"A;rm": "x"})


async def test_exec_before_start_is_an_infra_error() -> None:
    """Refuse a command before the box runs."""
    with pytest.raises(SandboxInfraError, match="not running"):
        await RunloopEnvironment(_spec()).exec("true")


async def test_a_timed_out_command_is_killed(fake_sdk: FakeCloud) -> None:
    """Kill the command and raise RunTimeoutError at the time limit."""
    async with RunloopEnvironment(_spec()) as env:
        fake_sdk.exec_hangs = True
        with pytest.raises(RunTimeoutError):
            await env.exec("true", timeout_sec=0.05)
    assert len(fake_sdk.killed) == 1


async def test_the_run_timeout_is_the_default_limit(fake_sdk: FakeCloud) -> None:
    """Use the run phase timeout when the call sets no limit."""
    async with RunloopEnvironment(_spec(), timeouts=_timeouts(run=0.05)) as env:
        fake_sdk.exec_hangs = True
        with pytest.raises(RunTimeoutError):
            await env.exec("true")
    assert len(fake_sdk.killed) == 1


async def test_a_cancelled_command_is_killed(fake_sdk: FakeCloud) -> None:
    """Kill the command when the caller cancels the exec."""
    async with RunloopEnvironment(_spec()) as env:
        fake_sdk.exec_hangs = True
        task = asyncio.ensure_future(env.exec("true"))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(fake_sdk.killed) == 1


async def test_upload_makes_the_parent_directories(fake_sdk: FakeCloud) -> None:
    """Make every parent directory in one command before the writes."""
    async with RunloopEnvironment(_spec()) as env:
        await env.upload_files({"/w/a/x.txt": "x", "/w/b/y.txt": "y", "/top.txt": "t"})
        assert await env.download_file("/w/b/y.txt") == "y"
        assert await env.download_file("/top.txt") == "t"
    assert sum(call.startswith("exec:") and "mkdir" in call for call in fake_sdk.calls) == 1


async def test_start_timeout_shuts_the_lost_box_down(fake_sdk: FakeCloud) -> None:
    """Raise StartTimeoutError and shut down the box the create left behind."""
    fake_sdk.create_hangs = True
    env = RunloopEnvironment(_spec(), timeouts=_timeouts(start=0.05))
    with pytest.raises(StartTimeoutError):
        await env.start()
    (box,) = fake_sdk.boxes.values()
    assert box.status == "shutdown"
    assert fake_sdk.calls[-1] == "aclose"


async def test_stop_during_start_shuts_the_box_down(fake_sdk: FakeCloud) -> None:
    """End a running start from another task and leak no box."""
    fake_sdk.create_hangs = True
    env = RunloopEnvironment(_spec())
    start = asyncio.ensure_future(env.start())
    await asyncio.sleep(0.01)
    await env.stop()
    with pytest.raises(SandboxInfraError, match="during its start"):
        await start
    (box,) = fake_sdk.boxes.values()
    assert box.status == "shutdown"


async def test_cancelled_start_shuts_the_box_down(fake_sdk: FakeCloud) -> None:
    """Keep the cancel and leak no box when the caller cancels the start."""
    fake_sdk.create_hangs = True
    start = asyncio.ensure_future(RunloopEnvironment(_spec()).start())
    await asyncio.sleep(0.01)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    (box,) = fake_sdk.boxes.values()
    assert box.status == "shutdown"


async def test_orphan_sweep_leaves_other_boxes_running(fake_sdk: FakeCloud) -> None:
    """Shut down only the box with this provider's own box tag."""
    other = RunloopEnvironment(_spec())
    await other.start()
    fake_sdk.create_hangs = True
    with pytest.raises(StartTimeoutError):
        await RunloopEnvironment(_spec(), timeouts=_timeouts(start=0.05)).start()
    assert [box.status for box in fake_sdk.boxes.values()] == ["running", "shutdown"]
    await other.stop()


async def test_orphan_sweep_stops_at_its_box(fake_sdk: FakeCloud) -> None:
    """Read no box after the tagged one, with large pages."""
    env = RunloopEnvironment(_spec())
    read: list[str] = []
    shut: list[str] = []

    async def listing(*, limit: int) -> AsyncIterator[SimpleNamespace]:
        """Yield the tagged box, then many other boxes."""
        assert limit == 5000
        yield SimpleNamespace(id="mine", metadata={BOX_TAG: env._box_tag}, status="running")
        for n in range(100):
            read.append(f"other-{n}")
            yield SimpleNamespace(id=f"other-{n}", metadata={}, status="running")

    async def shutdown(box_id: str) -> None:
        """Record the shutdown."""
        shut.append(box_id)

    client = SimpleNamespace(api=SimpleNamespace(devboxes=SimpleNamespace(list=listing, shutdown=shutdown)))
    await env._shut_down_orphan(client)
    assert shut == ["mine"]
    assert read == []


async def test_stop_job_sandboxes_bounds_the_list(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shut down the boxes found before a list that runs past the limit."""
    monkeypatch.setattr(runloop, "STOP_TIMEOUT", 0.05)
    env = RunloopEnvironment(_spec(tags={JOB_TAG: "job-5"}))
    await env.start()
    fake_sdk.list_hangs = True
    assert await RunloopEnvironment.stop_job_sandboxes("job-5") == 1
    assert fake_sdk.list_limits == [5000]
    assert fake_sdk.calls[-1] == "aclose"
    await env.stop()


async def test_stop_job_sandboxes_skips_ended_boxes(fake_sdk: FakeCloud) -> None:
    """Count only the running boxes of the job and close the client."""
    envs = [RunloopEnvironment(_spec(tags={JOB_TAG: "job-9"})) for _ in range(2)]
    for env in envs:
        await env.start()
    await envs[0].stop()
    assert await RunloopEnvironment.stop_job_sandboxes("job-9") == 1
    assert fake_sdk.calls[-1] == "aclose"
    await envs[1].stop()


async def test_stop_job_sandboxes_counts_only_successful_shutdowns(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leave a failed shutdown out of the count and go on."""
    envs = [RunloopEnvironment(_spec(tags={JOB_TAG: "job-7"})) for _ in range(2)]
    for env in envs:
        await env.start()
    calls = 0
    original = FakeSDK._shutdown

    async def flaky(self: FakeSDK, box_id: str) -> None:
        """Fail the first shutdown only."""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FakeInternalServerError("500")
        await original(self, box_id)

    monkeypatch.setattr(FakeSDK, "_shutdown", flaky)
    assert await RunloopEnvironment.stop_job_sandboxes("job-7") == 1
    for env in envs:
        await env.stop()
