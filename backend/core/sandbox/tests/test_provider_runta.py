"""Tests for the Runta sandbox provider, against an in-memory fake of the SDK.

No test reaches Runta: an autouse fixture swaps the SDK names in the
provider module for fakes and the lifetime clock for a gate, so the suite
needs no network, no credentials and no ``runta`` extra.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import shlex
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing.runta_usage import RETENTION_SEC
from core.billing.runtime import UsagePendingError
from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import NetworkPolicy
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.metering import usage_adapter
from core.sandbox.providers import runta
from core.sandbox.providers.runta import RuntaEnvironment
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.sandbox.timeouts import PhaseTimeouts

from .test_environment import SandboxEnvironmentContract


class FakeApiError(Exception):
    """Stand in for ``runta.ApiError``, which carries the HTTP status."""

    def __init__(self, status_code: int) -> None:
        """Keep the status.

        Args:
            status_code: The HTTP status that Runta answered.
        """
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeConfigError(Exception):
    """Stand in for ``runta.ConfigError``."""


class FakeEgressMode:
    """Stand in for ``runta.EgressMode``."""

    ALLOWLIST = "allowlist"
    DENYLIST = "denylist"


@dataclass
class FakeEgressPolicy:
    """Stand in for ``runta.EgressPolicy``."""

    mode: str = FakeEgressMode.DENYLIST
    allowed_hosts: list[str] = field(default_factory=list)
    denied_hosts: list[str] = field(default_factory=list)


@dataclass
class BoxState:
    """Hold one fake runtime in the fake cloud."""

    id: str
    params: dict[str, Any]
    deleted: bool = False
    egress: Any = None
    tcp: list[str] | None = None
    files: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=lambda: {"/", "/tmp"})


@dataclass
class FakeCloud:
    """Hold the shared state of the fake Runta account."""

    boxes: list[BoxState] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    create_calls: int = 0
    create_error: BaseException | None = None
    create_makes_box: bool = False
    create_gate: asyncio.Event | None = None
    create_started: asyncio.Event = field(default_factory=asyncio.Event)
    ready_error: BaseException | None = None
    ready_gate: asyncio.Event | None = None
    ready_started: asyncio.Event = field(default_factory=asyncio.Event)
    list_error: BaseException | None = None
    delete_fails: int = 0
    delete_calls: int = 0
    confirm_gate: Any = None
    confirm_error: BaseException | None = None
    pending_error: BaseException | None = None
    confirm_gate_entered: threading.Event = field(default_factory=threading.Event)
    exec_hangs: bool = False
    exec_exit: int | None = None
    closed: int = 0
    no_auth: bool = False
    cap: asyncio.Event = field(default_factory=asyncio.Event)
    cap_seconds: list[float] = field(default_factory=list)
    ids: Any = field(default_factory=lambda: itertools.count(1))

    def make_box(self, params: dict[str, Any]) -> BoxState:
        """Add one runtime to the cloud.

        Args:
            params: The create arguments.

        Returns:
            The new runtime.
        """
        box = BoxState(
            id=f"rt-{next(self.ids)}",
            params=params,
            egress=params.get("egress_policy"),
            tcp=params.get("tcp_allowlist"),
        )
        self.boxes.append(box)
        return box

    def run(self, box: BoxState, argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
        """Run the provider's argv in a tiny shell model.

        Args:
            box: The runtime.
            argv: The ``bash -c`` argv that ``exec`` received.
            env: The command environment.

        Returns:
            The exit code, stdout and stderr.
        """
        assert argv[:2] == ["bash", "-c"]
        words = shlex.split(argv[2])
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
            if args[0] in box.files:
                return 0, box.files[args[0]].decode("utf-8"), ""
            return 1, "", "No such file\n"
        if verb == "test" and args[0] == "-e":
            return (0, "", "") if args[1] in box.files or args[1] in box.dirs else (1, "", "")
        if verb == "mkdir" and args[0] == "-p":
            for path in args[1:]:
                parts = path.strip("/").split("/")
                box.dirs.update("/" + "/".join(parts[: n + 1]) for n in range(len(parts)))
            return 0, "", ""
        return 127, "", f"{verb}: command not found\n"


CLOUD = FakeCloud()


class FakeFiles:
    """Stand in for the async file manager of a runtime."""

    def __init__(self, box: BoxState) -> None:
        """Keep the runtime.

        Args:
            box: The runtime.
        """
        self.box = box

    async def write(self, path: str, data: bytes) -> None:
        """Write a file.

        Args:
            path: The absolute path.
            data: The file bytes.
        """
        self.box.files[path] = data

    async def read(self, path: str) -> bytes:
        """Read a file.

        Args:
            path: The absolute path.

        Returns:
            The file bytes.

        Raises:
            FakeApiError: The file does not exist.
        """
        if path not in self.box.files:
            raise FakeApiError(404)
        return self.box.files[path]


class FakeEgress:
    """Stand in for the async egress manager of a runtime."""

    def __init__(self, box: BoxState) -> None:
        """Keep the runtime.

        Args:
            box: The runtime.
        """
        self.box = box

    async def set_policy(self, policy: FakeEgressPolicy) -> None:
        """Change the egress policy.

        Args:
            policy: The new policy.
        """
        self.box.egress = policy

    async def set_tcp_allowlist(self, hosts: list[str]) -> None:
        """Replace the raw TCP allowlist.

        Args:
            hosts: The new list; empty denies raw TCP.
        """
        self.box.tcp = list(hosts)


class FakeRuntime:
    """Stand in for ``runta.AsyncRuntime``."""

    def __init__(self, box: BoxState) -> None:
        """Wrap one runtime of the fake cloud.

        Args:
            box: The runtime.
        """
        self.box = box
        self.files = FakeFiles(box)
        self.egress = FakeEgress(box)

    @property
    def id(self) -> str:
        """Return the runtime id."""
        return self.box.id

    @property
    def name(self) -> str:
        """Return the runtime display name."""
        return self.box.params["name"]

    async def wait_until_ready(self, *, timeout: float) -> None:  # noqa: ASYNC109 - the SDK signature
        """Wait for the runtime, or fail as the fake cloud sets.

        Args:
            timeout: The wait limit.

        Raises:
            BaseException: The error that the fake cloud sets.
        """
        self.box.params["ready_timeout"] = timeout
        CLOUD.ready_started.set()
        if CLOUD.ready_gate is not None:
            await CLOUD.ready_gate.wait()
        if CLOUD.ready_error is not None:
            raise CLOUD.ready_error

    async def delete(self) -> None:
        """Delete the runtime, or fail as the fake cloud sets.

        Raises:
            FakeApiError: The fake cloud fails this delete.
        """
        CLOUD.delete_calls += 1
        if CLOUD.delete_fails:
            CLOUD.delete_fails -= 1
            raise FakeApiError(503)
        self.box.deleted = True

    async def exec(
        self,
        command: list[str],
        timeout: float | None = None,  # noqa: ASYNC109 - the SDK signature
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        max_output_bytes: int | None = None,
    ) -> SimpleNamespace:
        """Run a command in the shell model and record the call.

        Args:
            command: The argv.
            timeout: The SDK timeout.
            env: The command environment.
            cwd: The working directory.
            max_output_bytes: The output limit.

        Returns:
            A fake ``CommandResult``.
        """
        assert not self.box.deleted
        CLOUD.runs.append(
            {"argv": command, "timeout": timeout, "env": env, "cwd": cwd, "max_output_bytes": max_output_bytes}
        )
        if CLOUD.exec_hangs:
            await asyncio.Event().wait()
        code, out, err = CLOUD.run(self.box, list(command), dict(env or {}))
        if CLOUD.exec_exit is not None:
            code = CLOUD.exec_exit
        return SimpleNamespace(
            exit_code=code, stdout_text=out, stderr_text=err, stdout_truncated=False, stderr_truncated=False
        )


class FakeRuntimes:
    """Stand in for ``AsyncRunta.runtimes``."""

    async def create(self, name: str, **params: Any) -> FakeRuntime:
        """Create a runtime, or fail as the fake cloud sets.

        Args:
            name: The runtime name.
            **params: The other create arguments.

        Returns:
            The runtime handle.

        Raises:
            BaseException: The create error that the fake cloud sets.
        """
        CLOUD.create_calls += 1
        CLOUD.create_started.set()
        if CLOUD.create_gate is not None:
            await CLOUD.create_gate.wait()
        params = {"name": name, **params}
        if CLOUD.create_error is not None:
            if CLOUD.create_makes_box:
                CLOUD.make_box(params)
            raise CLOUD.create_error
        return FakeRuntime(CLOUD.make_box(params))

    async def list(self) -> list[FakeRuntime]:
        """List the runtimes that are not deleted.

        Returns:
            The runtime handles.

        Raises:
            BaseException: The list error that the fake cloud sets.
        """
        if CLOUD.list_error is not None:
            raise CLOUD.list_error
        return [FakeRuntime(box) for box in CLOUD.boxes if not box.deleted]


class FakeRunta:
    """Stand in for ``runta.AsyncRunta``."""

    def __init__(self) -> None:
        """Open the fake client.

        Raises:
            FakeConfigError: The fake cloud says no credentials exist.
        """
        if CLOUD.no_auth:
            raise FakeConfigError("no token")
        self.runtimes = FakeRuntimes()

    async def close(self) -> None:
        """Close the fake client."""
        CLOUD.closed += 1


async def _fake_cap_sleep(seconds: float) -> None:
    """Wait for the test to end the box lifetime.

    Args:
        seconds: The lifetime that the provider asked for.
    """
    CLOUD.cap_seconds.append(seconds)
    await CLOUD.cap.wait()


@pytest.fixture(autouse=True)
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> FakeCloud:
    """Swap the Runta SDK and the lifetime clock for fakes, and reset the fake cloud.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fresh fake cloud.
    """
    global CLOUD
    CLOUD = FakeCloud()
    fakes = {
        "ApiError": FakeApiError,
        "AsyncRunta": FakeRunta,
        "ConfigError": FakeConfigError,
        "EgressMode": FakeEgressMode,
        "EgressPolicy": FakeEgressPolicy,
    }
    for name, value in fakes.items():
        monkeypatch.setattr(runta, name, value)
    monkeypatch.setenv("RUNTA_TOKEN", "test-token")
    monkeypatch.setattr(runta, "_cap_sleep", _fake_cap_sleep)
    monkeypatch.setattr(runta, "STOP_TIMEOUT", 1.0)
    monkeypatch.setattr(runta, "_JOB_BOXES", {})
    monkeypatch.setattr(runta, "_LATE_TASKS", set())
    return CLOUD


class TestRuntaEnvironmentContract(SandboxEnvironmentContract):
    """Run the shared provider contract against the fake Runta cloud."""

    provider_cls = RuntaEnvironment


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


async def _late_tasks_done() -> None:
    """Wait for the background tasks that outlive their box."""
    while runta._LATE_TASKS:
        await asyncio.wait(set(runta._LATE_TASKS))


class FakeUsage:
    """Record the reservation calls of a protected box."""

    def __init__(self, request: dict[str, Any], operation_key: str) -> None:
        """Keep the priced request and the operation key.

        Args:
            request: The request that the provider priced.
            operation_key: The box's operation key.
        """
        self.request = request
        self.operation_key = operation_key
        self.calls: list[tuple[str, Any]] = [("reserve", CLOUD.create_calls)]
        self.runtime_id: str | None = None
        self.settle_error: BaseException | None = None
        self.confirm_error: BaseException | None = CLOUD.confirm_error
        self.pending_error: BaseException | None = CLOUD.pending_error
        # Threading events, because the provider calls the ledger from a thread.
        self.confirm_gate: threading.Event | None = CLOUD.confirm_gate
        self.pending_gate: threading.Event | None = None
        self.pending_entered = threading.Event()

    def sent(self, name: str) -> None:
        """Record the create call, with the creates that ran before it.

        Args:
            name: The unique name of the runtime.
        """
        self.calls.append(("sent", CLOUD.create_calls))

    def confirm_created(self, handle: Any) -> None:
        """Record the runtime id.

        Args:
            handle: The runtime handle.

        Raises:
            BaseException: The error that the test sets.
        """
        self.calls.append(("confirm", handle.id))
        CLOUD.confirm_gate_entered.set()
        if self.confirm_gate is not None:
            self.confirm_gate.wait(5)
        if self.confirm_error is not None:
            raise self.confirm_error
        self.runtime_id = handle.id

    def stopped(self) -> None:
        """Record the end of the clock, with the box state at that time."""
        self.calls.append(("stopped", [box.deleted for box in CLOUD.boxes]))

    def settle(self) -> None:
        """Record a settle, or fail as the test sets.

        Raises:
            BaseException: The error that the test sets.
        """
        if self.settle_error is not None:
            raise self.settle_error
        self.calls.append(("settle", [box.deleted for box in CLOUD.boxes]))

    def fail(self, absent: bool = False) -> None:
        """Record a failed create.

        Args:
            absent: True when Runta refused the create and the lookup found nothing.
        """
        self.calls.append(("fail", absent))

    def pending(self) -> None:
        """Record a pending hold, wait at the gate, or fail, as the test sets.

        Raises:
            BaseException: The error that the test sets.
        """
        self.calls.append(("pending", None))
        self.pending_entered.set()
        if self.pending_gate is not None:
            self.pending_gate.wait(5)
        if self.pending_error is not None:
            raise self.pending_error

    def names(self) -> list[str]:
        """Return the names of the recorded calls.

        Returns:
            The call names in order.
        """
        return [name for name, _ in self.calls]


def _protected(timeouts: PhaseTimeouts | None = None, **spec: Any) -> tuple[RuntaEnvironment, list[FakeUsage]]:
    """Open a protected box whose reservations are fakes.

    Args:
        timeouts: The phase timeouts.
        **spec: Spec fields to replace.

    Returns:
        The box and the list that gets its reservation.
    """
    made: list[FakeUsage] = []

    def factory_(budget: Any, request: dict[str, Any], *, operation_key: str) -> FakeUsage:
        """Make and keep one fake reservation.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.

        Returns:
            The fake reservation.
        """
        assert budget == "budget"
        made.append(FakeUsage(request, operation_key))
        return made[-1]

    env = RuntaEnvironment(
        _spec(operation_key="box-1", **spec), timeouts=timeouts, budget="budget", usage_factory=factory_
    )
    return env, made


def test_runta_is_billable_and_has_a_usage_adapter() -> None:
    """Let hosted Skynet meter Runta: the provider is billable and its adapter exists."""
    assert runta.RUNTA_CAPABILITIES.billable is True
    assert usage_adapter("runta") is not None
    assert factory.PROVIDERS["runta"].production_allowed is False


def test_runta_is_not_allowed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the Runta provider in production before its import.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(factory.ProductionNotAllowedError, match="runta"):
        factory.load_provider("runta")


def test_factory_loads_runta_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load the Runta class from the registry outside production.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(factory.settings, "app_environment", "staging")
    assert factory.load_provider("runta") is RuntaEnvironment


def test_preflight_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the extra when the SDK is not installed.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(runta, "AsyncRunta", None)
    with pytest.raises(SandboxFatalError, match="runta"):
        RuntaEnvironment.preflight()


def test_preflight_needs_a_token_or_a_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Refuse to start without a token and without a config file.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        tmp_path: A temporary directory.
    """
    monkeypatch.delenv("RUNTA_TOKEN")
    monkeypatch.setenv("RUNTA_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(SandboxFatalError, match="RUNTA_TOKEN"):
        RuntaEnvironment.preflight()
    (tmp_path / "missing.toml").write_text("")
    RuntaEnvironment.preflight()


def test_a_spec_image_is_refused() -> None:
    """Refuse a spec image, because the direct path runs Runta's default image."""
    with pytest.raises(SandboxSpecError, match="image"):
        RuntaEnvironment(_spec(image="python:3.12"))


def test_a_protected_box_needs_an_operation_key() -> None:
    """Refuse a budget without an operation key, before any call."""
    with pytest.raises(SandboxSpecError, match="operation key"):
        RuntaEnvironment(_spec(), budget="budget")


def test_a_protected_box_over_the_lifetime_ceiling_is_refused() -> None:
    """Refuse a protected box that Skynet cannot price, before any call."""
    with pytest.raises(SandboxSpecError, match="cannot fund"):
        RuntaEnvironment(_spec(lifetime_seconds=2 * 86_400, operation_key="k"), budget="budget")


async def test_create_sends_equal_limits_and_the_lifetime_knobs(fake_sdk: FakeCloud) -> None:
    """Send equal requests and limits, the session limit at the lifetime, and the retention.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    async with RuntaEnvironment(_spec(lifetime_seconds=300.5, name="eval")):
        pass
    params = fake_sdk.boxes[0].params
    assert params["name"].startswith("eval-")
    assert params["resources"] == {
        "requests": {"vcpus": 1, "memory_mib": 2048, "disk_gib": 16},
        "limits": {"vcpus": 1, "memory_mib": 2048},
    }
    assert params["auto_shutdown_after_secs"] == 301
    assert params["auto_delete_after_secs"] == RETENTION_SEC
    assert params["wait"] is False
    assert params["egress_policy"] == FakeEgressPolicy(mode="denylist")
    assert "tcp_allowlist" not in params
    assert fake_sdk.cap_seconds == [301]
    assert fake_sdk.boxes[0].deleted
    assert fake_sdk.closed == 1


async def test_sizes_map_to_whole_units(fake_sdk: FakeCloud) -> None:
    """Map memory and disk to MiB and whole GiB, with the 16 GiB disk floor.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    resources = ResourceRequest(cpus=2, memory_mb=4096, disk_mb=40 * 1024 + 1)
    async with RuntaEnvironment(_spec(resources=resources)):
        pass
    assert fake_sdk.boxes[0].params["resources"]["requests"] == {"vcpus": 2, "memory_mib": 4096, "disk_gib": 41}


@pytest.mark.parametrize(
    ("network", "expected"),
    [
        ({"network_disabled": True}, FakeEgressPolicy(mode="allowlist")),
        (
            {"allowed_hosts": ("a.example", "a.example")},
            FakeEgressPolicy(mode="allowlist", allowed_hosts=["a.example"]),
        ),
    ],
)
async def test_the_spec_network_maps_to_the_create_egress(
    fake_sdk: FakeCloud, network: dict[str, Any], expected: FakeEgressPolicy
) -> None:
    """Send a deny-all or allowlist egress policy and an empty raw TCP list with the create.

    Args:
        fake_sdk: The fake Runta cloud.
        network: The spec network fields.
        expected: The expected egress policy.
    """
    async with RuntaEnvironment(_spec(**network)):
        pass
    assert fake_sdk.boxes[0].params["egress_policy"] == expected
    assert fake_sdk.boxes[0].params["tcp_allowlist"] == []


async def test_set_network_policy_changes_the_box_egress(fake_sdk: FakeCloud) -> None:
    """Send a new egress policy and an empty raw TCP list to the running runtime.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    async with RuntaEnvironment(_spec()) as env:
        await env.set_network_policy(NetworkPolicy(allowed_hosts=("b.example",)))
        assert fake_sdk.boxes[0].egress == FakeEgressPolicy(mode="allowlist", allowed_hosts=["b.example"])
        assert fake_sdk.boxes[0].tcp == []


async def test_exec_sends_argv_env_timeout_and_cwd(fake_sdk: FakeCloud) -> None:
    """Send a bash argv, the merged env, a whole-second timeout, the cwd and the output limit.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    async with RuntaEnvironment(_spec(env={"A": "1"}), timeouts=_timeouts(run=2.5)) as env:
        await env.exec("true", cwd="/tmp", env={"B": "2"})
    assert fake_sdk.runs[0] == {
        "argv": ["bash", "-c", "true"],
        "timeout": 3,
        "env": {"A": "1", "B": "2"},
        "cwd": "/tmp",
        "max_output_bytes": runta.MAX_OUTPUT_BYTES,
    }


async def test_exit_124_with_a_limit_is_a_timeout(fake_sdk: FakeCloud) -> None:
    """Report the GNU timeout exit code as a timed-out result.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    async with RuntaEnvironment(_spec(), timeouts=_timeouts()) as env:
        fake_sdk.exec_exit = 124
        result = await env.exec("true")
    assert result.timed_out
    assert result.exit_code == 124


async def test_a_hung_exec_returns_a_timed_out_result(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """End a hung SDK call after the limit and the grace.

    Args:
        fake_sdk: The fake Runta cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(runta, "EXEC_GRACE", 0.01)
    async with RuntaEnvironment(_spec()) as env:
        fake_sdk.exec_hangs = True
        result = await env.exec("true", timeout_sec=0.01)
    assert result.timed_out


async def test_a_missing_download_is_probed(fake_sdk: FakeCloud) -> None:
    """Return None when the read fails and the probe finds no file.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    async with RuntaEnvironment(_spec()) as env:
        assert await env.download_file("/tmp/nothing") is None
    assert fake_sdk.runs[-1]["argv"] == ["bash", "-c", "test -e /tmp/nothing"]


async def test_a_non_utf8_download_is_infra(fake_sdk: FakeCloud) -> None:
    """Refuse a file that is not UTF-8 text.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    async with RuntaEnvironment(_spec()) as env:
        fake_sdk.boxes[0].files["/tmp/bin"] = b"\xff"
        with pytest.raises(SandboxInfraError, match="UTF-8"):
            await env.download_file("/tmp/bin")


async def test_a_protected_box_reserves_before_the_create_and_settles_after_the_delete(
    fake_sdk: FakeCloud,
) -> None:
    """Reserve the whole lifetime first, bind the runtime, and settle once it is deleted.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    env, made = _protected(lifetime_seconds=60)
    async with env:
        pass
    usage = made[0]
    assert usage.operation_key == "box-1"
    assert usage.request == {"lifetime_ms": 60_000, "vcpus": 1, "memory_mib": 2048, "disk_gib": 16}
    assert usage.calls == [
        ("reserve", 0),
        ("sent", 0),
        ("confirm", "rt-1"),
        ("stopped", [True]),
        ("settle", [True]),
    ]


async def test_the_lifetime_cap_deletes_and_settles_the_box(fake_sdk: FakeCloud) -> None:
    """Delete and settle the box when its funded lifetime ends, and refuse later commands.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    env, made = _protected(lifetime_seconds=90)
    await env.start()
    assert fake_sdk.cap_seconds == [90]
    fake_sdk.cap.set()
    await asyncio.wait({env._cap_task})
    assert not env._cap_task.cancelled()
    await asyncio.wait({env._stop_task})
    assert made[0].names() == ["reserve", "sent", "confirm", "stopped", "settle"]
    with pytest.raises(SandboxInfraError, match="not running"):
        await env.exec("true")
    await env.stop()
    assert made[0].names().count("settle") == 1


async def test_a_failed_start_deletes_and_settles(fake_sdk: FakeCloud) -> None:
    """Delete and settle the box when it fails to run.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.ready_error = RuntimeError("crashed")
    env, made = _protected()
    with pytest.raises(SandboxInfraError, match="failed to start"):
        await env.start()
    assert made[0].names() == ["reserve", "sent", "confirm", "stopped", "settle"]
    await env.stop()


async def test_a_start_timeout_deletes_and_settles(fake_sdk: FakeCloud) -> None:
    """End the start at its limit, then delete and settle the box.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.ready_gate = asyncio.Event()
    env, made = _protected(timeouts=_timeouts(start=0.05))
    with pytest.raises(StartTimeoutError):
        await env.start()
    assert made[0].names()[-2:] == ["stopped", "settle"]
    assert fake_sdk.boxes[0].deleted


async def test_a_cancelled_start_settles_and_keeps_the_cancel(fake_sdk: FakeCloud) -> None:
    """Delete and settle the box when the caller cancels the start, and raise the cancel.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.ready_gate = asyncio.Event()
    env, made = _protected()
    task = asyncio.ensure_future(env.start())
    await fake_sdk.ready_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert made[0].names()[-2:] == ["stopped", "settle"]
    assert fake_sdk.boxes[0].deleted


async def test_a_stop_during_start_settles(fake_sdk: FakeCloud) -> None:
    """End the start with an infra error when a stop comes, and settle the box.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.ready_gate = asyncio.Event()
    env, made = _protected()
    task = asyncio.ensure_future(env.start())
    await fake_sdk.ready_started.wait()
    await env.stop()
    with pytest.raises(SandboxInfraError, match="stopped during its start"):
        await task
    assert made[0].names()[-2:] == ["stopped", "settle"]


async def test_a_refused_create_without_a_box_releases(fake_sdk: FakeCloud) -> None:
    """Release the hold when Runta refused the create and the lookup finds no runtime.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.create_error = FakeApiError(422)
    env, made = _protected()
    with pytest.raises(SandboxInfraError, match="create the runtime"):
        await env.start()
    assert made[0].calls[-1] == ("fail", True)


async def test_an_argument_error_releases(fake_sdk: FakeCloud) -> None:
    """Release the hold when the SDK refused the arguments before any request.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.create_error = ValueError("bad resources")
    env, made = _protected()
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert made[0].calls[-1] == ("fail", True)


@pytest.mark.parametrize("error", [FakeApiError(500), FakeApiError(429), ConnectionError("reset")])
async def test_an_unknown_create_stays_pending(fake_sdk: FakeCloud, error: BaseException) -> None:
    """Keep the hold when the create outcome is unknown and the lookup finds no runtime.

    Args:
        fake_sdk: The fake Runta cloud.
        error: The create error.
    """
    fake_sdk.create_error = error
    env, made = _protected()
    with pytest.raises(SandboxInfraError):
        await env.start()
    # The reservation keeps a sent create pending when ``absent`` is False.
    assert made[0].calls[-1] == ("fail", False)


async def test_a_failed_create_that_made_a_box_is_deleted_and_settled(fake_sdk: FakeCloud) -> None:
    """Find the runtime of a failed create by name, delete it and settle it.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.create_error = FakeApiError(422)
    fake_sdk.create_makes_box = True
    env, made = _protected()
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert made[0].names() == ["reserve", "sent", "confirm", "stopped", "settle"]
    assert fake_sdk.boxes[0].deleted


async def test_a_failed_lookup_keeps_the_hold(fake_sdk: FakeCloud) -> None:
    """Keep the hold pending when the lookup after a failed create fails.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.create_error = FakeApiError(422)
    fake_sdk.list_error = FakeApiError(503)
    env, made = _protected()
    with pytest.raises(SandboxInfraError):
        await env.start()
    assert made[0].names() == ["reserve", "sent", "pending"]
    with pytest.raises(UsagePendingError, match="could not list"):
        await env.stop()


async def test_a_failed_delete_stays_pending_and_a_later_stop_settles(fake_sdk: FakeCloud) -> None:
    """Keep the hold and the handle when the delete fails, and settle on the next stop.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    env, made = _protected()
    await env.start()
    fake_sdk.delete_fails = 1
    with pytest.raises(UsagePendingError, match="did not delete"):
        await env.stop()
    assert made[0].names()[-1] == "pending"
    assert not fake_sdk.boxes[0].deleted
    await env.stop()
    assert made[0].names()[-2:] == ["stopped", "settle"]
    assert fake_sdk.boxes[0].deleted


async def test_a_settle_failure_reaches_stop(fake_sdk: FakeCloud) -> None:
    """Raise a settle failure from stop after the box is deleted.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    env, made = _protected()
    await env.start()
    made[0].settle_error = UsagePendingError("ledger down")
    with pytest.raises(UsagePendingError, match="ledger down"):
        await env.stop()
    assert fake_sdk.boxes[0].deleted


async def test_a_create_past_the_grace_is_deleted_and_settled_late(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the hold pending and hand a create that outlives its grace to a task that deletes and settles it.

    Args:
        fake_sdk: The fake Runta cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(runta, "CREATE_GRACE", 0.01)
    fake_sdk.create_gate = asyncio.Event()
    env, made = _protected(timeouts=_timeouts(start=0.02))
    with pytest.raises(StartTimeoutError):
        await env.start()
    assert made[0].names() == ["reserve", "sent", "pending"]
    fake_sdk.create_gate.set()
    await _late_tasks_done()
    assert made[0].names() == ["reserve", "sent", "pending", "confirm", "stopped", "settle"]
    assert fake_sdk.boxes[0].deleted


async def test_a_missing_token_releases_the_unsent_hold(fake_sdk: FakeCloud) -> None:
    """Release the hold when the client cannot open, because no create was sent.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.no_auth = True
    env, made = _protected()
    with pytest.raises(SandboxFatalError, match="credentials"):
        await env.start()
    assert made[0].calls == [("reserve", 0), ("fail", False)]
    assert fake_sdk.create_calls == 0


async def test_a_refused_reserve_sends_no_create(fake_sdk: FakeCloud) -> None:
    """Send no create when the budget refuses the hold.

    Args:
        fake_sdk: The fake Runta cloud.
    """

    def refuse(budget: Any, request: dict[str, Any], *, operation_key: str) -> None:
        """Refuse the reservation.

        Args:
            budget: The budget runtime.
            request: The priced request.
            operation_key: The box's operation key.

        Raises:
            UsagePendingError: Always.
        """
        raise UsagePendingError("no budget")

    env = RuntaEnvironment(_spec(operation_key="k"), budget="budget", usage_factory=refuse)
    with pytest.raises(UsagePendingError, match="no budget"):
        await env.start()
    assert fake_sdk.create_calls == 0
    assert fake_sdk.closed == 0


async def test_an_sdk_error_while_waiting_is_infra_and_settles(fake_sdk: FakeCloud) -> None:
    """Map an SDK error of the ready wait to an infra error, and delete and settle the box.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.ready_error = FakeApiError(503)
    env, made = _protected()
    with pytest.raises(SandboxInfraError, match="wait for the runtime: FakeApiError"):
        await env.start()
    assert made[0].names() == ["reserve", "sent", "confirm", "stopped", "settle"]
    assert fake_sdk.boxes[0].deleted


async def test_a_stop_during_the_pending_call_shares_the_teardown(fake_sdk: FakeCloud) -> None:
    """Start no second teardown while the first one marks the hold pending after a failed delete.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    env, made = _protected()
    await env.start()
    usage = made[0]
    usage.pending_gate = threading.Event()
    fake_sdk.delete_fails = 1
    first = asyncio.ensure_future(env.stop())
    await asyncio.to_thread(usage.pending_entered.wait, 5)
    second = asyncio.ensure_future(env.stop())
    await asyncio.sleep(0)
    usage.pending_gate.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert sum(isinstance(result, UsagePendingError) for result in results) == 1
    assert fake_sdk.delete_calls == 1
    assert usage.names().count("pending") == 1
    await env.stop()
    assert usage.names()[-2:] == ["stopped", "settle"]


async def test_the_lifetime_cap_logs_a_failed_settle(fake_sdk: FakeCloud, caplog: pytest.LogCaptureFixture) -> None:
    """Log the pending hold when the delete at the lifetime cap fails.

    Args:
        fake_sdk: The fake Runta cloud.
        caplog: The log capture.
    """
    env, made = _protected()
    await env.start()
    fake_sdk.delete_fails = 1
    with caplog.at_level("WARNING", logger=runta.__name__):
        fake_sdk.cap.set()
        await asyncio.wait({env._cap_task})
    assert "did not settle at its lifetime: UsagePendingError" in caplog.text
    assert made[0].names()[-1] == "pending"
    await env.stop()
    assert made[0].names()[-2:] == ["stopped", "settle"]


async def test_a_cancel_during_the_confirm_waits_for_the_ledger_thread(fake_sdk: FakeCloud) -> None:
    """Let the confirm thread end before the teardown settles, so the confirm runs once.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.confirm_gate = threading.Event()
    env, made = _protected()
    task = asyncio.ensure_future(env.start())
    await asyncio.to_thread(fake_sdk.confirm_gate_entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    fake_sdk.confirm_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert made[0].names() == ["reserve", "sent", "confirm", "stopped", "settle"]


async def _late_create(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> FakeUsage:
    """End a start whose create outlives its grace, and return its reservation.

    Args:
        fake_sdk: The fake Runta cloud.
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The reservation, which the background task owns now.
    """
    monkeypatch.setattr(runta, "CREATE_GRACE", 0.01)
    fake_sdk.create_gate = asyncio.Event()
    env, made = _protected(timeouts=_timeouts(start=0.02))
    with pytest.raises(StartTimeoutError):
        await env.start()
    assert made[0].names() == ["reserve", "sent", "pending"]
    return made[0]


async def test_a_late_failed_confirm_still_deletes_and_settles(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete a late runtime when its confirm fails, then settle it as the teardown does.

    Args:
        fake_sdk: The fake Runta cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    usage = await _late_create(fake_sdk, monkeypatch)
    usage.confirm_error = UsagePendingError("ledger down")
    fake_sdk.create_gate.set()
    await _late_tasks_done()
    assert fake_sdk.boxes[0].deleted
    assert usage.names() == ["reserve", "sent", "pending", "confirm", "stopped", "settle"]


async def test_a_late_refused_create_without_a_box_releases(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Look up a late refused create by name, and release the hold when no runtime exists.

    Args:
        fake_sdk: The fake Runta cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    usage = await _late_create(fake_sdk, monkeypatch)
    fake_sdk.create_error = FakeApiError(422)
    fake_sdk.create_gate.set()
    await _late_tasks_done()
    assert usage.calls[-1] == ("fail", True)


async def test_a_late_unknown_create_stays_pending(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the hold of a late create with an unknown outcome when no runtime has its name.

    Args:
        fake_sdk: The fake Runta cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    usage = await _late_create(fake_sdk, monkeypatch)
    fake_sdk.create_error = FakeApiError(500)
    fake_sdk.create_gate.set()
    await _late_tasks_done()
    assert usage.calls[-1] == ("fail", False)


async def test_a_late_failed_create_that_made_a_box_is_deleted_and_settled(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Find the runtime of a late failed create by name, then delete and settle it.

    Args:
        fake_sdk: The fake Runta cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    usage = await _late_create(fake_sdk, monkeypatch)
    fake_sdk.create_error = FakeApiError(422)
    fake_sdk.create_makes_box = True
    fake_sdk.create_gate.set()
    await _late_tasks_done()
    assert fake_sdk.boxes[0].deleted
    assert usage.names() == ["reserve", "sent", "pending", "confirm", "stopped", "settle"]


async def test_a_late_failed_lookup_keeps_the_hold(fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the pending hold when the lookup after a late failed create fails.

    Args:
        fake_sdk: The fake Runta cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    usage = await _late_create(fake_sdk, monkeypatch)
    fake_sdk.create_error = FakeApiError(422)
    fake_sdk.list_error = FakeApiError(503)
    fake_sdk.create_gate.set()
    await _late_tasks_done()
    assert usage.names() == ["reserve", "sent", "pending"]


async def test_a_ledger_error_in_the_confirm_still_deletes_and_settles(fake_sdk: FakeCloud) -> None:
    """Map a ledger error of the confirm to an infra error, then still delete and settle the box.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    fake_sdk.confirm_error = ConnectionError("ledger down")
    env, made = _protected()
    with pytest.raises(SandboxInfraError, match="confirm the runtime in the ledger") as caught:
        await env.start()
    assert isinstance(caught.value.__cause__, ConnectionError)
    assert fake_sdk.boxes[0].deleted
    assert made[0].names() == ["reserve", "sent", "confirm", "confirm", "stopped", "settle"]


async def test_a_failed_pending_call_still_hands_off_the_late_create(
    fake_sdk: FakeCloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete and settle a late runtime even when the hand-off cannot mark the hold pending.

    Args:
        fake_sdk: The fake Runta cloud.
        monkeypatch: The pytest monkeypatch fixture.
    """
    fake_sdk.pending_error = ConnectionError("ledger down")
    monkeypatch.setattr(runta, "CREATE_GRACE", 0.01)
    fake_sdk.create_gate = asyncio.Event()
    env, made = _protected(timeouts=_timeouts(start=0.02))
    with pytest.raises(StartTimeoutError):
        await env.start()
    with pytest.raises(UsagePendingError, match="late runtime pending") as caught:
        await env.stop()
    assert isinstance(caught.value.__cause__, ConnectionError)
    fake_sdk.create_gate.set()
    await _late_tasks_done()
    assert fake_sdk.boxes[0].deleted
    assert fake_sdk.closed == 1
    assert made[0].names() == ["reserve", "sent", "pending", "confirm", "stopped", "settle"]


def test_the_runtime_name_is_cleaned_and_short() -> None:
    """Keep a long, odd spec name to Runta's characters and 63 characters, with the random suffix."""
    env = RuntaEnvironment(_spec(name="My Eval/Run:" + "X" * 100))
    assert len(env._name) == 63
    assert re.fullmatch(r"my-eval-run-x+-[0-9a-f]{12}", env._name)
    assert RuntaEnvironment(_spec(name="//"))._name.startswith("skynet-")


async def test_a_failed_delete_blocks_commands_but_not_a_later_stop(fake_sdk: FakeCloud) -> None:
    """Refuse commands after a failed delete, and let a later stop delete and settle the box.

    Args:
        fake_sdk: The fake Runta cloud.
    """
    env, made = _protected()
    await env.start()
    fake_sdk.delete_fails = 1
    with pytest.raises(UsagePendingError):
        await env.stop()
    for call in (env.exec("true"), env.upload_files({"/tmp/a": "a"}), env.download_file("/tmp/a")):
        with pytest.raises(SandboxInfraError, match="not running"):
            await call
    await env.stop()
    assert made[0].names()[-2:] == ["stopped", "settle"]


async def test_a_failed_tcp_close_leaves_the_egress_policy_unchanged(fake_sdk: FakeCloud) -> None:
    """Close raw TCP before the egress policy changes, so a failure opens nothing.

    Args:
        fake_sdk: The fake Runta cloud.
    """

    async def broken(hosts: list[str]) -> None:
        """Fail the raw TCP change.

        Args:
            hosts: Ignored.

        Raises:
            FakeApiError: Always.
        """
        raise FakeApiError(503)

    async with RuntaEnvironment(_spec()) as env:
        before = fake_sdk.boxes[0].egress
        runtime = env._runtime
        runtime.egress.set_tcp_allowlist = broken
        with pytest.raises(SandboxInfraError, match="egress policy"):
            await env.set_network_policy(NetworkPolicy(disabled=True))
        assert fake_sdk.boxes[0].egress == before
