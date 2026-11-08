"""Tests for the smolmachines sandbox provider, against an in-memory fake of the smolmachines SDK.

No test calls smolmachines: an autouse fixture replaces every SDK name that
the provider uses with the fakes below.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from types import SimpleNamespace
from typing import Any

import pytest

from core.sandbox import (
    JOB_TAG,
    NetworkPolicy,
    ResourceMode,
    ResourceRequest,
    SandboxEnvironment,
    SandboxSpec,
    SandboxSpecError,
    factory,
)
from core.sandbox.errors import RunTimeoutError, SandboxFatalError
from core.sandbox.factory import ProductionNotAllowedError
from core.sandbox.providers import smol as smol_provider
from core.sandbox.providers.smol import SmolEnvironment, machine_name, network_args, resource_spec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract
from core.sandbox.timeouts import PhaseTimeouts

_SPEC = SandboxSpec(lifetime_seconds=60)


class FakeSmolError(Exception):
    """Stand in for ``smol.errors.SmolError``, which carries a string code."""

    def __init__(self, code: str) -> None:
        """Keep the code.

        Args:
            code: The SDK error code.
        """
        super().__init__(code)
        self.code = code


class FakeMachine:
    """Act as a smolmachines ``AsyncMachine`` with a dict file system and a few shell commands."""

    def __init__(self, sdk: FakeSmol, config: Any, connect: Any) -> None:
        """Record the create arguments.

        Args:
            sdk: The fake SDK that made the machine.
            config: The ``MachineConfig`` of the create.
            connect: The ``ConnectOptions`` of the create.
        """
        self.sdk = sdk
        self.config = config
        self.connect = connect
        self.files: dict[str, bytes] = {}
        self.execs: list[tuple[list[str], Any]] = []
        self.deleted = 0
        self.delete_error: Exception | None = None

    async def exec(self, argv: list[str], options: Any) -> SimpleNamespace:
        """Run one fake shell command.

        Args:
            argv: The argv; ``argv[4]`` is the shell command.
            options: The ``ExecOptions``.

        Returns:
            The stdout, stderr and exit code.
        """
        self.execs.append((argv, options))
        if self.sdk.exec_hook is not None:
            await self.sdk.exec_hook()
        command = argv[4]
        words = shlex.split(command)
        env = dict(self.config.env or {}) | dict(getattr(options, "env", None) or {})
        out, err, code = "", "", 0
        match words:
            case ["true"]:
                pass
            case ["false"]:
                code = 1
            case ["exit", number]:
                code = int(number)
            case ["echo", *rest]:
                out = " ".join(rest) + "\n"
            case ["printenv", name]:
                if name in env:
                    out = env[name] + "\n"
                else:
                    code = 1
            case ["cat", path]:
                if path in self.files:
                    out = self.files[path].decode("utf-8")
                else:
                    err, code = f"cat: {path}: No such file\n", 1
            case ["test", "-f", path]:
                code = 0 if path in self.files else 1
            case ["mkdir", "-p", *_]:
                pass
            case _:
                err, code = f"unknown command: {command}\n", 127
        return SimpleNamespace(stdout=out, stderr=err, exit_code=code)

    async def write_file(self, path: str, data: bytes) -> None:
        """Store one file.

        Args:
            path: The machine path.
            data: The file bytes.
        """
        self.files[path] = data

    async def read_file(self, path: str) -> bytes:
        """Return one file.

        Args:
            path: The machine path.

        Returns:
            The file bytes.
        """
        return self.files[path]

    async def delete(self) -> None:
        """Delete the machine, or raise the configured error.

        Raises:
            Exception: The configured ``delete_error``.
        """
        self.deleted += 1
        if self.sdk.delete_gate is not None:
            await self.sdk.delete_gate.wait()
        if self.delete_error is not None:
            raise self.delete_error


class FakeSmol:
    """Act as the ``AsyncMachine`` class and keep every machine it makes."""

    def __init__(self) -> None:
        """Start with no machines and no injected faults."""
        self.machines: list[FakeMachine] = []
        self.creates = 0
        self.create_error: Exception | None = None
        self.create_gate: asyncio.Event | None = None
        self.create_entered = asyncio.Event()
        self.delete_gate: asyncio.Event | None = None
        self.exec_hook: Any = None

    async def create(self, config: Any, connect: Any) -> FakeMachine:
        """Make one machine, after the gate opens.

        Args:
            config: The ``MachineConfig``.
            connect: The ``ConnectOptions``.

        Returns:
            The new machine.

        Raises:
            Exception: The configured ``create_error``.
        """
        self.creates += 1
        self.create_entered.set()
        if self.create_gate is not None:
            await self.create_gate.wait()
        if self.create_error is not None:
            raise self.create_error
        machine = FakeMachine(self, config, connect)
        self.machines.append(machine)
        return machine


def _record(**kwargs: Any) -> SimpleNamespace:
    """Stand in for an SDK options type and keep its fields.

    Args:
        **kwargs: The fields.

    Returns:
        An object with the fields as attributes.
    """
    return SimpleNamespace(**kwargs)


@pytest.fixture(autouse=True)
def fake_smol(monkeypatch: pytest.MonkeyPatch) -> FakeSmol:
    """Replace the smolmachines SDK names in the provider module with fakes.

    Returns:
        The fake SDK.
    """
    sdk = FakeSmol()
    monkeypatch.setattr(smol_provider, "AsyncMachine", SimpleNamespace(create=sdk.create))
    monkeypatch.setattr(smol_provider, "SmolError", FakeSmolError)
    for name in ("ConnectOptions", "ExecOptions", "MachineConfig", "ResourceSpec"):
        monkeypatch.setattr(smol_provider, name, _record)
    monkeypatch.setattr(smol_provider, "_LIVE", set())
    return sdk


class TestSmolEnvironmentContract(SandboxEnvironmentContract):
    """Run the shared provider contract against the fake SDK."""

    provider_cls = SmolEnvironment


async def test_create_sends_the_name_image_env_and_connection(fake_smol: FakeSmol) -> None:
    """Normalize the name and send the image, env, ready timeout and target."""
    spec = SandboxSpec(lifetime_seconds=60, name="My Job_7!", image="ubuntu:24.04", env={"A": "1"})
    timeouts = PhaseTimeouts(start=42, setup=1, run=1, score=1)
    async with SmolEnvironment(spec, target="cloud", api_key="k", base_url="https://smol.test", timeouts=timeouts):
        machine = fake_smol.machines[0]
        assert machine.config.name == "my-job-7"
        assert machine.config.image == "ubuntu:24.04"
        assert machine.config.env == {"A": "1"}
        assert machine.config.ready_timeout_seconds == 42
        assert vars(machine.connect) == {"target": "cloud", "api_key": "k", "base_url": "https://smol.test"}


async def test_create_defaults(fake_smol: FakeSmol) -> None:
    """Use the default image, a generated name, no env and the local target."""
    async with SmolEnvironment(_SPEC):
        machine = fake_smol.machines[0]
        assert machine.config.image == smol_provider.DEFAULT_IMAGE
        assert machine.config.name.startswith("skynet-")
        assert machine.config.env is None
        assert machine.config.ready_timeout_seconds == smol_provider.DEFAULT_READY_TIMEOUT_SEC
        assert machine.connect.target == "local"


def test_machine_name_falls_back_and_caps_the_length() -> None:
    """Fall back to a fixed name and keep at most 128 characters."""
    assert machine_name("!!!") == "skynet"
    assert len(machine_name("a" * 300)) == 128


@pytest.mark.parametrize(
    ("mode", "cpus", "memory", "storage"),
    [
        (ResourceMode.REQUEST, 3, 2048, 2),
        (ResourceMode.LIMIT, 3, 2048, 2),
        (ResourceMode.IGNORE, None, None, None),
    ],
)
def test_resource_spec_follows_the_resource_mode(mode: ResourceMode, cpus: Any, memory: Any, storage: Any) -> None:
    """Send the sizes, with the disk in whole GB, unless the mode ignores them."""
    request = ResourceRequest(cpus=3, memory_mb=2048, disk_mb=1025, mode=mode)
    resources = resource_spec(SandboxSpec(lifetime_seconds=60, resources=request), NetworkPolicy())
    assert (resources.cpus, resources.memory_mb, resources.storage_gb) == (cpus, memory, storage)
    assert resources.cuda is False


def test_resource_spec_sends_one_gpu_as_cuda() -> None:
    """Turn one requested GPU into the CUDA flag."""
    spec = SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(gpu=1))
    assert resource_spec(spec, NetworkPolicy()).cuda is True


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (NetworkPolicy(), (True, [], [])),
        (NetworkPolicy(disabled=True), (False, [], [])),
        (
            NetworkPolicy(allowed_hosts=("pypi.org", "10.0.0.1", "192.168.0.0/16")),
            (True, ["pypi.org"], ["10.0.0.1/32", "192.168.0.0/16"]),
        ),
    ],
)
def test_network_args_split_hosts_and_cidrs(policy: NetworkPolicy, expected: tuple) -> None:
    """Map open, deny-all and allowlist egress to the smolmachines arguments."""
    assert network_args(policy) == expected


async def test_create_applies_the_spec_network_policy(fake_smol: FakeSmol) -> None:
    """Send the allowlist of the spec at create time."""
    spec = SandboxSpec(lifetime_seconds=60, allowed_hosts=["pypi.org"])
    async with SmolEnvironment(spec):
        resources = fake_smol.machines[0].config.resources
        assert (resources.network, resources.allow_hosts, resources.allow_cidrs) == (True, ["pypi.org"], None)


@pytest.mark.parametrize(
    ("spec", "target"),
    [
        (SandboxSpec(lifetime_seconds=60, allowed_hosts=["*.example.com"]), "local"),
        (SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(gpu=2)), "local"),
        (SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(gpu=1)), "cloud"),
        (_SPEC, "mars"),
    ],
)
def test_specs_smol_cannot_apply_are_refused(spec: SandboxSpec, target: Any) -> None:
    """Refuse wildcard hosts, more than one GPU, cloud GPUs and unknown targets."""
    with pytest.raises(SandboxSpecError):
        SmolEnvironment(spec, target=target)


async def test_exec_sends_cwd_env_user_and_the_run_phase_timeout(fake_smol: FakeSmol) -> None:
    """Merge the env, send the cwd and the root user, and round the run timeout up."""
    timeouts = PhaseTimeouts(start=1, setup=1, run=2.5, score=1)
    spec = SandboxSpec(lifetime_seconds=60, env={"A": "1", "B": "1"})
    async with SmolEnvironment(spec, timeouts=timeouts) as env:
        await env.exec("true", cwd="/work", env={"B": "2"}, sudo=True)
        argv, options = fake_smol.machines[0].execs[-1]
    assert argv == ["/bin/sh", "-c", smol_provider._SHELL_EXEC, "skynet-smol", "true"]
    assert options.workdir == "/work"
    assert options.env == {"A": "1", "B": "2"}
    assert options.user == "root"
    assert options.timeout == 3


async def test_exec_without_a_limit_sends_no_timeout_or_user(fake_smol: FakeSmol) -> None:
    """Send no timeout and the image user when nothing sets them."""
    async with SmolEnvironment(_SPEC) as env:
        await env.exec("true")
        _, options = fake_smol.machines[0].execs[-1]
    assert (options.timeout, options.user, options.env) == (None, None, None)


async def test_exec_before_start_raises() -> None:
    """Refuse a command before the machine runs."""
    with pytest.raises(RuntimeError, match="not running"):
        await SmolEnvironment(_SPEC).exec("true")


async def test_local_deadline_bounds_a_hung_exec(fake_smol: FakeSmol, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise a run timeout when the SDK exec hangs past the limit."""
    monkeypatch.setattr(smol_provider, "_TIMEOUT_GRACE_SEC", 0.0)
    async with SmolEnvironment(_SPEC) as env:
        fake_smol.exec_hook = asyncio.Event().wait
        with pytest.raises(RunTimeoutError):
            await env.exec("true", timeout_sec=0.05)


async def test_sdk_error_past_the_limit_becomes_a_run_timeout(fake_smol: FakeSmol) -> None:
    """Treat an SDK error that arrives at the limit as a run timeout."""

    async def slow_failure() -> None:
        await asyncio.sleep(0.06)
        raise FakeSmolError("EXEC_TIMEOUT")

    async with SmolEnvironment(_SPEC) as env:
        fake_smol.exec_hook = slow_failure
        with pytest.raises(RunTimeoutError):
            await env.exec("true", timeout_sec=0.05)


async def test_sdk_error_before_the_limit_passes_on(fake_smol: FakeSmol) -> None:
    """Keep an early SDK error as it is."""

    async def failure() -> None:
        raise FakeSmolError("CONNECTION_LOST")

    async with SmolEnvironment(_SPEC) as env:
        fake_smol.exec_hook = failure
        with pytest.raises(FakeSmolError):
            await env.exec("true", timeout_sec=30)


async def test_upload_creates_parents_as_root_and_download_keeps_crlf(fake_smol: FakeSmol) -> None:
    """Create the parent directories as root, and return the exact file text."""
    async with SmolEnvironment(_SPEC) as env:
        await env.upload_files({"/a b/x.txt": "one\r\ntwo", "/a b/y.txt": "", "/top.txt": "t"})
        machine = fake_smol.machines[0]
        argv, options = machine.execs[0]
        assert argv[4] == "mkdir -p '/a b'"
        assert options.user == "root"
        assert await env.download_file("/a b/x.txt") == "one\r\ntwo"


async def test_failed_mkdir_raises(fake_smol: FakeSmol) -> None:
    """Raise when the parent directories cannot be created."""

    async def no_mkdir() -> None:
        raise RuntimeError("mkdir refused")

    async with SmolEnvironment(_SPEC) as env:
        fake_smol.exec_hook = no_mkdir
        with pytest.raises(RuntimeError, match="mkdir refused"):
            await env.upload_files({"/d/x.txt": "x"})


async def test_failed_create_runs_once_with_no_retry(fake_smol: FakeSmol) -> None:
    """Call the SDK create once and pass its error on (SKY-406 D3, D10)."""
    fake_smol.create_error = FakeSmolError("QUOTA")
    env = SmolEnvironment(_SPEC)
    with pytest.raises(FakeSmolError):
        await env.start()
    assert fake_smol.creates == 1
    assert not smol_provider._LIVE


async def test_start_is_a_no_op_while_running(fake_smol: FakeSmol) -> None:
    """Create one machine for two start calls."""
    async with SmolEnvironment(_SPEC) as env:
        await env.start()
    assert fake_smol.creates == 1


async def test_stop_ignores_a_machine_that_is_gone(fake_smol: FakeSmol) -> None:
    """Count a not-found delete as a stop."""
    env = SmolEnvironment(_SPEC)
    await env.start()
    fake_smol.machines[0].delete_error = FakeSmolError("VM_NOT_FOUND")
    await env.stop()
    assert fake_smol.machines[0].deleted == 1


async def test_stop_raises_other_delete_errors_once(fake_smol: FakeSmol) -> None:
    """Pass on a delete error that is not a not-found, and do not retry it."""
    env = SmolEnvironment(_SPEC)
    await env.start()
    fake_smol.machines[0].delete_error = FakeSmolError("INTERNAL")
    with pytest.raises(FakeSmolError):
        await env.stop()
    assert fake_smol.machines[0].deleted == 1


async def test_cancel_during_start_deletes_the_new_machine(fake_smol: FakeSmol) -> None:
    """Wait for a cancelled create to finish, then delete the machine it made."""
    fake_smol.create_gate = asyncio.Event()
    env = SmolEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await fake_smol.create_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    fake_smol.create_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake_smol.machines[0].deleted == 1
    with pytest.raises(RuntimeError):
        await env.exec("true")


async def test_cancel_during_a_hung_create_deletes_the_late_machine(
    fake_smol: FakeSmol, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass the cancel on after the grace time, and delete the machine when the create ends."""
    monkeypatch.setattr(smol_provider, "_CANCEL_GRACE_SEC", 0.01)
    fake_smol.create_gate = asyncio.Event()
    env = SmolEnvironment(_SPEC)
    task = asyncio.create_task(env.start())
    await fake_smol.create_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake_smol.machines == []
    fake_smol.create_gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert fake_smol.machines[0].deleted == 1
    assert not smol_provider._LATE_DELETES


async def test_cancel_during_stop_still_deletes(fake_smol: FakeSmol, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finish the delete in the background when a stop is cancelled past the grace time."""
    monkeypatch.setattr(smol_provider, "_CANCEL_GRACE_SEC", 0.01)
    env = SmolEnvironment(_SPEC)
    await env.start()
    fake_smol.delete_gate = asyncio.Event()
    task = asyncio.create_task(env.stop())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(smol_provider._LATE_DELETES) == 1
    fake_smol.delete_gate.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert not smol_provider._LATE_DELETES
    assert fake_smol.machines[0].deleted == 1


async def test_stop_job_sandboxes_continues_after_a_failure(
    fake_smol: FakeSmol, caplog: pytest.LogCaptureFixture
) -> None:
    """Log a failed delete, go on with the next machine, and count only the deleted ones."""
    envs = [SmolEnvironment(SandboxSpec(lifetime_seconds=60, tags={JOB_TAG: "job-9"})) for _ in range(2)]
    for env in envs:
        await env.start()
    fake_smol.machines[0].delete_error = FakeSmolError("INTERNAL")
    with caplog.at_level(logging.ERROR):
        assert await SmolEnvironment.stop_job_sandboxes("job-9") == 1
    assert "job-9" in caplog.text
    assert all(machine.deleted == 1 for machine in fake_smol.machines)


async def test_exec_sends_output_to_the_sink(fake_smol: FakeSmol) -> None:
    """Send stdout and stderr to the sink after the command ends."""
    chunks: list[tuple[str, str]] = []
    async with SmolEnvironment(_SPEC) as env:
        await env.exec("cat /missing", on_output=lambda stream, text: chunks.append((stream, text)))
    assert chunks == [("stderr", "cat: /missing: No such file\n")]


def test_preflight_needs_the_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise a fatal error that names the extra when the SDK is missing."""
    monkeypatch.setattr(smol_provider, "AsyncMachine", None)
    with pytest.raises(SandboxFatalError, match="smol"):
        SmolEnvironment.preflight()


def test_factory_creates_the_smol_provider_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build a SmolEnvironment through the factory in dev, and run its preflight."""
    monkeypatch.setattr(factory.settings, "app_environment", "development")
    environment = factory.create_environment("smol", _SPEC)
    assert isinstance(environment, SmolEnvironment)
    assert isinstance(environment, SandboxEnvironment)
    factory.preflight("smol")


def test_factory_refuses_the_smol_provider_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the dev-only smol provider in production, for create and preflight (SKY-406 D1)."""
    assert factory.PROVIDERS["smol"].production_allowed is False
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        factory.create_environment("smol", _SPEC)
    with pytest.raises(ProductionNotAllowedError):
        factory.preflight("smol")


def test_capabilities_allow_every_network_mode_and_gpus() -> None:
    """Report static egress for all modes, GPUs, and no billing or protection."""
    caps = SmolEnvironment.capabilities
    assert caps.name == "smol"
    assert not caps.dynamic_network_policy
    assert caps.gpus
    assert not caps.billable
    assert not caps.protected
    assert len(caps.network_modes) == 3
