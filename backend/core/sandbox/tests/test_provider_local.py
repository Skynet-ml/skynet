"""Tests for the local subprocess sandbox provider."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import pytest

from core.sandbox import NetworkMode, SandboxEnvironment, SandboxSpec, SandboxSpecError, factory
from core.sandbox.errors import RunTimeoutError, SandboxRejectedError
from core.sandbox.factory import ProductionNotAllowedError
from core.sandbox.providers import local
from core.sandbox.providers.local import LocalEnvironment
from core.sandbox.tests.test_environment import SandboxEnvironmentContract
from core.sandbox.timeouts import PhaseTimeouts

_SPEC = SandboxSpec(lifetime_seconds=60)


class TestLocalEnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the local provider."""

    provider_cls = LocalEnvironment

    async def test_upload_then_download_round_trips(self, sandbox: SandboxEnvironment) -> None:
        """Read back the exact text that an upload wrote, with paths inside the box.

        The local box has no root file system of its own, so the contract's
        absolute ``/tmp`` paths would be host paths. They are refused instead.
        """
        await sandbox.upload_files({"contract/a.txt": "alpha\r\nבטא\n"})
        assert await sandbox.download_file("contract/a.txt") == "alpha\r\nבטא\n"
        assert (await sandbox.exec("cat contract/a.txt")).stdout == "alpha\r\nבטא\n"

    async def test_download_missing_file_returns_none(self, sandbox: SandboxEnvironment) -> None:
        """Return None for a path inside the box that does not exist."""
        assert await sandbox.download_file("contract/missing.txt") is None


def _alive(pid: int) -> bool:
    """Say whether a process with this pid still exists.

    Args:
        pid: The process id.

    Returns:
        True while the process exists.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_until_gone(pid: int, seconds: float = 10.0) -> bool:
    """Poll until a process is gone.

    Args:
        pid: The process id.
        seconds: The longest wait.

    Returns:
        True when the process is gone before the wait ends.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


async def _read_pid(box: LocalEnvironment, path: str) -> int:
    """Wait for a command to write a pid file into the box, then read it.

    Args:
        box: The running sandbox.
        path: The pid file path inside the box.

    Returns:
        The pid.
    """
    for _ in range(200):
        text = await box.download_file(path)
        if text and text.strip():
            return int(text)
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} was not written")


async def test_exec_returns_stdout_stderr_and_exit_code() -> None:
    """Return both output streams and the exact exit code."""
    async with LocalEnvironment(_SPEC) as box:
        result = await box.exec("echo out; echo err >&2; exit 7")
    assert result.exit_code == 7
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert not result.timed_out


async def test_exec_runs_in_the_box_with_a_scrubbed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start in the temp directory, apply spec and call env, and drop host variables."""
    monkeypatch.setenv("SKY412_HOST_ONLY", "leak")
    async with LocalEnvironment(SandboxSpec(lifetime_seconds=60, env={"FROM_SPEC": "spec"})) as box:
        result = await box.exec(
            'echo "$PWD|$HOME|$FROM_SPEC|$FROM_CALL|${SKY412_HOST_ONLY:-unset}"', env={"FROM_CALL": "call"}
        )
        root = box.path
    assert result.stdout.strip() == f"{root}|{root}|spec|call|unset"


async def test_exec_cwd_is_inside_the_box() -> None:
    """Run in a subdirectory of the box and refuse a cwd outside it."""
    async with LocalEnvironment(_SPEC) as box:
        await box.upload_files({"sub/f.txt": "x"})
        assert (await box.exec("ls", cwd="sub")).stdout == "f.txt\n"
        with pytest.raises(ValueError, match="leaves"):
            await box.exec("true", cwd="..")


async def test_paths_outside_the_box_are_refused() -> None:
    """Refuse absolute host paths and parent-directory escapes for file transfer."""
    async with LocalEnvironment(_SPEC) as box:
        with pytest.raises(ValueError, match="leaves"):
            await box.upload_files({"../escape.txt": "x"})
        with pytest.raises(ValueError, match="leaves"):
            await box.download_file("/etc/hosts")
        assert box.path is not None
        inside = str(box.path / "abs.txt")
        await box.upload_files({inside: "ok"})
        assert await box.download_file("abs.txt") == "ok"


async def test_sudo_is_rejected() -> None:
    """Never run a local command as root."""
    async with LocalEnvironment(_SPEC) as box:
        with pytest.raises(SandboxRejectedError, match="root"):
            await box.exec("true", sudo=True)


async def test_exec_before_start_raises() -> None:
    """Refuse a command when the box is not running."""
    with pytest.raises(RuntimeError, match="not running"):
        await LocalEnvironment(_SPEC).exec("true")


async def test_timeout_kills_the_process_group_and_raises_run_timeout() -> None:
    """Kill the shell and its grandchild on timeout, then raise the run-timeout error."""
    async with LocalEnvironment(_SPEC) as box:
        task = asyncio.create_task(box.exec("sleep 60 & echo $! > child.pid; wait", timeout_sec=1.0))
        child = await _read_pid(box, "child.pid")
        assert _alive(child)
        with pytest.raises(RunTimeoutError) as caught:
            await task
        assert caught.value.seconds == 1.0
        assert await _wait_until_gone(child)


async def test_run_phase_timeout_is_the_default_limit() -> None:
    """Use the run phase of the phase timeouts when the call gives no limit."""
    timeouts = PhaseTimeouts(start=10, setup=10, run=0.5, score=10, multiplier=2.0)
    async with LocalEnvironment(_SPEC, timeouts=timeouts) as box:
        with pytest.raises(RunTimeoutError) as caught:
            await box.exec("sleep 30")
        assert caught.value.seconds == 1.0
        assert (await box.exec("true", timeout_sec=30)).ok


async def test_cancelled_exec_kills_the_process_group_and_stop_removes_the_dir() -> None:
    """Kill the command group on a cancel, and remove the box when the block exits."""
    box = LocalEnvironment(_SPEC)
    started = asyncio.Event()
    holder: dict[str, object] = {}

    async def _work() -> None:
        """Run a long command inside the box until the test cancels it."""
        async with box:
            holder["root"] = box.path
            started.set()
            await box.exec("sleep 60 & echo $! > child.pid; wait")

    task = asyncio.create_task(_work())
    await started.wait()
    child = await _read_pid(box, "child.pid")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    root = holder["root"]
    assert isinstance(root, Path)
    assert not root.exists()
    assert box.path is None
    assert await _wait_until_gone(child)


async def test_stop_finishes_the_removal_when_it_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the directory before a cancelled stop passes the cancel on."""
    real_rmtree = local.shutil.rmtree

    def _slow_rmtree(path: Path) -> None:
        """Remove the tree after a delay, so the cancel lands mid-removal.

        Args:
            path: The directory to remove.
        """
        time.sleep(0.3)
        real_rmtree(path)

    box = LocalEnvironment(_SPEC)
    await box.start()
    root = box.path
    assert root is not None
    monkeypatch.setattr(local.shutil, "rmtree", _slow_rmtree)
    task = asyncio.create_task(box.stop())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not root.exists()


async def test_stop_kills_running_commands() -> None:
    """End a running command when another task stops the box."""
    box = LocalEnvironment(_SPEC)
    await box.start()
    task = asyncio.create_task(box.exec("sleep 60 & echo $! > child.pid; wait"))
    child = await _read_pid(box, "child.pid")
    await box.stop()
    result = await asyncio.wait_for(task, 10)
    assert not result.ok
    assert await _wait_until_gone(child)


async def test_on_output_receives_both_streams() -> None:
    """Send stdout and stderr chunks to the sink, tagged by stream."""
    chunks: list[tuple[str, str]] = []
    async with LocalEnvironment(_SPEC) as box:
        await box.exec("echo a; echo b >&2", on_output=lambda stream, text: chunks.append((stream, text)))
    assert "".join(text for stream, text in chunks if stream == "stdout") == "a\n"
    assert "".join(text for stream, text in chunks if stream == "stderr") == "b\n"


def test_capabilities_report_no_network_enforcement() -> None:
    """Offer only open egress, with no policy change and no header injection."""
    caps = LocalEnvironment.capabilities
    assert caps.name == "local"
    assert caps.network_modes == frozenset({NetworkMode.OPEN})
    assert not caps.dynamic_network_policy
    assert not caps.injects_headers
    assert not caps.protected
    assert not caps.billable


@pytest.mark.parametrize(
    "spec",
    [
        SandboxSpec(lifetime_seconds=60, network_disabled=True),
        SandboxSpec(lifetime_seconds=60, allowed_hosts=("example.com",)),
    ],
)
def test_specs_that_need_network_enforcement_are_refused(spec: SandboxSpec) -> None:
    """Refuse a deny-all or allowlist spec instead of running it with open egress."""
    with pytest.raises(SandboxSpecError, match="network mode"):
        LocalEnvironment(spec)


def test_preflight_says_the_network_is_not_enforced(caplog: pytest.LogCaptureFixture) -> None:
    """Warn that the local provider enforces no network policy."""
    with caplog.at_level(logging.WARNING, logger=local.__name__):
        LocalEnvironment.preflight()
    assert "does not enforce a network policy" in caplog.text


def test_factory_creates_the_local_provider_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build a LocalEnvironment through the factory in dev, and pass constructor options."""
    monkeypatch.setattr(factory.settings, "app_environment", "development")
    timeouts = PhaseTimeouts(start=1, setup=1, run=3, score=1)
    environment = factory.create_environment("local", _SPEC, timeouts=timeouts)
    assert isinstance(environment, LocalEnvironment)
    assert environment.spec is _SPEC
    factory.preflight("local")


def test_factory_refuses_the_local_provider_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the dev-only local provider in production, for create and preflight."""
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        factory.create_environment("local", _SPEC)
    with pytest.raises(ProductionNotAllowedError):
        factory.preflight("local")
