"""Tests for the async Singularity / Apptainer sandbox provider.

A fake ``apptainer`` script replaces the real CLI. ``pull`` writes a stub
image file. ``exec`` maps the ``-B`` bind to its host directory and runs the
bootstrap and the real command server on this machine, so every request goes
through the real server over loopback. No test starts a container or reaches
the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, SandboxEnvironment
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.factory import PROVIDERS, ProductionNotAllowedError, create_environment, load_provider
from core.sandbox.providers import singularity as singularity_provider
from core.sandbox.providers.singularity import TOKEN_HEADER, SingularityEnvironment
from core.sandbox.resources import ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

IMAGE = "docker.io/library/python:3.12-slim"
STATE_ENV = "SKYNET_FAKE_SINGULARITY_STATE"

FAKE_APPTAINER = textwrap.dedent(
    """
    import json, os, sys, time
    from pathlib import Path

    state = Path(os.environ["SKYNET_FAKE_SINGULARITY_STATE"])
    args = sys.argv[1:]
    with open(state / "calls.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps(args) + "\\n")
    verb = args[0] if args else ""

    def fail(name):
        if (state / name).exists():
            sys.stderr.write(f"FATAL: fake {name}\\n")
            sys.exit(1)

    if verb == "version":
        fail("version_fail")
        print("1.3.0")
    elif verb == "pull":
        fail("pull_fail")
        Path(args[-2]).write_bytes(b"SIF")
    elif verb == "exec":
        (state / "exec.pid").write_text(str(os.getpid()))
        if (state / "idle").exists():
            time.sleep(3600)
        fail("exec_fail")
        binds = {}
        rest = args[1:]
        index = 0
        while rest[index].startswith("-"):
            if rest[index] in ("-B", "--bind"):
                host, guest = rest[index + 1].split(":", 1)
                binds[guest] = host
                index += 2
            elif rest[index] == "--no-mount":
                index += 2
            else:
                index += 1
        command = rest[index + 1 :]
        for guest, host in binds.items():
            command = [host + arg[len(guest) :] if arg.startswith(guest) else arg for arg in command]
        env = {
            "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        for key, value in os.environ.items():
            for prefix in ("APPTAINERENV_", "SINGULARITYENV_"):
                if key.startswith(prefix):
                    env[key[len(prefix) :]] = value
        os.execvpe(command[0], command, env)
    else:
        sys.stderr.write(f"fake apptainer: unknown call {args}\\n")
        sys.exit(2)
    """
)


@pytest.fixture
def fake_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake ``apptainer`` and point the provider at it.

    Args:
        tmp_path: The test's directory.
        monkeypatch: Sets the class defaults and the fake's state directory.

    Returns:
        The fake's state directory.
    """
    state = tmp_path / "fake-state"
    state.mkdir()
    fake_bin = tmp_path / "bin" / "apptainer"
    fake_bin.parent.mkdir()
    fake_bin.write_text(f"#!{sys.executable}\n{FAKE_APPTAINER}")
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(STATE_ENV, str(state))
    monkeypatch.setattr(SingularityEnvironment, "default_bin", str(fake_bin))
    monkeypatch.setattr(SingularityEnvironment, "default_workdir", str(tmp_path / "app"))
    monkeypatch.setattr(SingularityEnvironment, "image_cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(SingularityEnvironment, "poll_seconds", 0.02)
    return state


def _calls(state: Path) -> list[list[str]]:
    """Read every CLI call that the fake logged.

    Args:
        state: The fake's state directory.

    Returns:
        The argument lists, in order.
    """
    path = state / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _verbs(state: Path) -> list[str]:
    """Return the verb of every logged CLI call.

    Args:
        state: The fake's state directory.

    Returns:
        The verbs, in order.
    """
    return [call[0] for call in _calls(state)]


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small spec with an image.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), "image": IMAGE, **overrides})


def _alive(pid: int) -> bool:
    """Return whether a process exists and is not a zombie that is waiting to be reaped.

    Args:
        pid: The process id.

    Returns:
        True when the process still runs.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_dead(pid: int, seconds: float = 10.0) -> bool:
    """Wait until a process is gone.

    Args:
        pid: The process id.
        seconds: The longest wait.

    Returns:
        True when the process ended in time.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return False


def _has_text(path: Path) -> bool:
    """Return whether a file exists and is not empty.

    Args:
        path: The file.

    Returns:
        True when the file has text.
    """
    return path.exists() and bool(path.read_text().strip())


async def _wait_for_file(path: Path, seconds: float = 10.0) -> None:
    """Wait until a file exists and is not empty.

    Args:
        path: The file.
        seconds: The longest wait.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_has_text, path):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} did not appear")


@pytest.mark.usefixtures("fake_state")
class TestSingularityEnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake ``apptainer``."""

    provider_cls = SingularityEnvironment

    def make_spec(self, **overrides: Any) -> SandboxSpec:
        """Return the contract spec with the test image.

        Args:
            **overrides: Spec fields to replace.

        Returns:
            The spec.
        """
        return super().make_spec(**{"image": IMAGE, **overrides})

    async def test_upload_then_download_round_trips(self, sandbox: SandboxEnvironment) -> None:
        """Read back the exact text, through a path relative to the sandbox workdir.

        The fake runs on this machine, so ``/tmp`` would be the host's ``/tmp``.
        """
        await sandbox.upload_files({"contract/a.txt": "alpha\r\nבטא\n"})
        assert await sandbox.download_file("contract/a.txt") == "alpha\r\nבטא\n"
        assert (await sandbox.exec("cat contract/a.txt")).stdout == "alpha\r\nבטא\n"

    async def test_download_missing_file_returns_none(self, sandbox: SandboxEnvironment) -> None:
        """Return None for a relative path that does not exist."""
        assert await sandbox.download_file("contract/missing.txt") is None


def test_factory_registers_singularity_for_development_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the provider with no extra, before openshift, and refuse it in production."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    entry = PROVIDERS["singularity"]
    assert (entry.module, entry.cls, entry.extra, entry.production_allowed) == (
        "core.sandbox.providers.singularity",
        "SingularityEnvironment",
        None,
        False,
    )
    assert list(PROVIDERS)[-2:] == ["singularity", "openshift"]
    assert load_provider("singularity") is SingularityEnvironment
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        load_provider("singularity")


def test_factory_creates_the_provider_with_its_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass provider kwargs through ``create_environment``."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    env = create_environment(
        "singularity", _spec(image=None), image=IMAGE, workdir="/work", singularity_bin="singularity", fakeroot=False
    )
    assert isinstance(env, SingularityEnvironment)
    assert (env._image, env._workdir, env._bin, env._fakeroot) == (IMAGE, "/work", "singularity", False)


def test_an_image_is_required() -> None:
    """Refuse a sandbox with no image."""
    with pytest.raises(SandboxSpecError, match="needs an image"):
        SingularityEnvironment(_spec(image=None))


def test_a_gpu_is_refused() -> None:
    """Refuse a GPU, which the provider does not declare."""
    with pytest.raises(SandboxSpecError):
        SingularityEnvironment(_spec(resources=ResourceRequest(cpus=1, gpu=1)))


def test_preflight_passes_when_the_cli_answers(fake_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``apptainer version`` through the factory and return when it succeeds."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    factory.preflight("singularity")
    assert _verbs(fake_state) == ["version"]


def test_preflight_fails_when_the_cli_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Raise SandboxFatalError when neither CLI is on PATH."""
    monkeypatch.setattr(SingularityEnvironment, "default_bin", str(tmp_path / "no-apptainer"))
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SandboxFatalError, match="CLI on PATH"):
        SingularityEnvironment.preflight()


def test_preflight_fails_when_version_fails(fake_state: Path) -> None:
    """Raise SandboxFatalError when ``version`` exits non-zero."""
    (fake_state / "version_fail").write_text("")
    with pytest.raises(SandboxFatalError, match="does not work"):
        SingularityEnvironment.preflight()


async def test_start_runs_the_container_with_isolation_flags_and_no_token_on_argv(fake_state: Path) -> None:
    """Start with ``--containall``, no bind paths, a writable tmpfs and fakeroot; keep the token off argv."""
    async with SingularityEnvironment(_spec()) as env:
        assert env._client is not None
        token = env._client.headers[TOKEN_HEADER]
    exec_call = next(call for call in _calls(fake_state) if call[0] == "exec")
    assert exec_call[1:6] == ["--containall", "--no-mount", "bind-paths", "--writable-tmpfs", "--fakeroot"]
    assert "--lifetime-seconds" in exec_call
    assert all(token not in arg for call in _calls(fake_state) for arg in call)


async def test_the_client_uses_the_port_that_the_server_wrote(fake_state: Path) -> None:
    """Connect to the port from the server's port file; pass no port on argv."""
    async with SingularityEnvironment(_spec()) as env:
        assert env._client is not None
        assert env._staging is not None
        written = int((env._staging / "port").read_text())
        assert env._client.base_url.port == written
    exec_call = next(call for call in _calls(fake_state) if call[0] == "exec")
    assert "--port" not in exec_call
    assert exec_call[exec_call.index("--port-file") + 1] == "/staging/port"


async def test_no_request_or_token_goes_out_before_the_port_file(tmp_path: Path) -> None:
    """Send nothing to a listener on any port until the server writes its port file."""
    seen: list[tuple[str, int | None, str | None]] = []

    def imposter(request: httpx.Request) -> httpx.Response:
        """Accept every request, like a process that took a free port.

        Args:
            request: The request.

        Returns:
            A 200 answer.
        """
        seen.append((request.url.path, request.url.port, request.headers.get(TOKEN_HEADER)))
        return httpx.Response(200, json={"ok": True})

    class StillBooting:
        """Stand in for a container process that has not exited."""

        returncode = None

    env = SingularityEnvironment(_spec(), singularity_bin="/bin/true", transport=httpx.MockTransport(imposter))
    env.poll_seconds = 0.02
    env.ready_seconds = 0.2
    env._process = StillBooting()  # type: ignore[assignment]
    with pytest.raises(StartTimeoutError):
        await env._wait_ready(tmp_path, "SECRET-TOKEN")
    assert seen == []
    assert env._client is None
    (tmp_path / "port").write_text("43210")
    env.ready_seconds = 5.0
    await env._wait_ready(tmp_path, "SECRET-TOKEN")
    assert seen == [("/health", 43210, "SECRET-TOKEN")]
    assert env._client is not None
    await env._client.aclose()


async def test_a_missing_server_file_raises_fatal_and_leaves_nothing(
    fake_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Map an OSError from staging the server files to SandboxFatalError, with no container."""
    empty = tmp_path / "no-package-data"
    empty.mkdir()
    monkeypatch.setattr(singularity_provider, "_HERE", empty)
    env = SingularityEnvironment(_spec())
    with pytest.raises(SandboxFatalError, match="could not be staged"):
        await env.start()
    assert _verbs(fake_state) == []
    assert env not in singularity_provider._LIVE
    assert env._staging is not None
    assert not env._staging.exists()


async def test_fakeroot_can_be_turned_off(fake_state: Path) -> None:
    """Leave out ``--fakeroot`` when the caller asks."""
    async with SingularityEnvironment(_spec(), fakeroot=False):
        pass
    exec_call = next(call for call in _calls(fake_state) if call[0] == "exec")
    assert "--fakeroot" not in exec_call


async def test_the_image_cache_pulls_once(fake_state: Path, tmp_path: Path) -> None:
    """Pull an image once and reuse the cached file for the next sandbox."""
    for _ in range(2):
        async with SingularityEnvironment(_spec()):
            pass
    assert _verbs(fake_state).count("pull") == 1
    pull = next(call for call in _calls(fake_state) if call[0] == "pull")
    assert pull[-1] == f"docker://{IMAGE}"
    cached = list((tmp_path / "cache").iterdir())
    assert len(cached) == 1
    assert not cached[0].name.endswith(".partial.sif")


async def test_a_local_sif_skips_the_pull(fake_state: Path, tmp_path: Path) -> None:
    """Use a local ``.sif`` file as it is."""
    image = tmp_path / "task.sif"
    image.write_bytes(b"SIF")
    async with SingularityEnvironment(_spec(image=str(image))):
        pass
    assert "pull" not in _verbs(fake_state)
    exec_call = next(call for call in _calls(fake_state) if call[0] == "exec")
    assert str(image) in exec_call


async def test_a_failed_pull_raises_once_and_leaves_nothing(fake_state: Path, tmp_path: Path) -> None:
    """Raise SandboxInfraError for a failed pull, with no retry and nothing left behind."""
    (fake_state / "pull_fail").write_text("")
    env = SingularityEnvironment(_spec())
    with pytest.raises(SandboxInfraError, match="pull"):
        await env.start()
    assert _verbs(fake_state) == ["pull"]
    assert env not in singularity_provider._LIVE
    assert env._staging is not None
    assert not env._staging.exists()
    assert list((tmp_path / "cache").iterdir()) == []


async def test_a_timeout_returns_124_and_kills_the_command(fake_state: Path) -> None:
    """Return exit code 124 with ``timed_out`` and keep a plain ``exit 124`` apart."""
    async with SingularityEnvironment(_spec()) as env:
        started = time.monotonic()
        result = await env.exec("sleep 30", timeout_sec=0.5)
        assert time.monotonic() - started < 10
        assert (result.exit_code, result.timed_out) == (124, True)
        plain = await env.exec("exit 124")
        assert (plain.exit_code, plain.timed_out) == (124, False)


async def test_cwd_is_relative_to_the_workdir(fake_state: Path, tmp_path: Path) -> None:
    """Run in a relative cwd under the workdir and fail for a missing cwd."""
    async with SingularityEnvironment(_spec()) as env:
        assert (await env.exec("mkdir -p sub")).ok
        result = await env.exec("pwd", cwd="sub")
        assert result.stdout.strip().endswith(f"{tmp_path.name}/app/sub")
        assert not (await env.exec("true", cwd="missing")).ok


async def test_the_server_refuses_a_bad_token(fake_state: Path) -> None:
    """Answer 403 to a request with the wrong token."""
    async with SingularityEnvironment(_spec()) as env:
        assert env._client is not None
        async with httpx.AsyncClient(base_url=str(env._client.base_url)) as other:
            assert (await other.get("/health", headers={TOKEN_HEADER: "wrong"})).status_code == 403
            assert (await other.post("/exec", json={"id": "x", "command": "true"})).status_code == 403


async def test_commands_do_not_see_the_token(fake_state: Path) -> None:
    """Keep the server token out of the command environment."""
    async with SingularityEnvironment(_spec()) as env:
        assert env._client is not None
        token = env._client.headers[TOKEN_HEADER]
        result = await env.exec("env")
        assert result.ok
        assert token not in result.stdout
        assert "SKYNET_SERVER_TOKEN" not in result.stdout


async def test_a_client_timeout_kills_the_command_and_raises_infra(fake_state: Path) -> None:
    """Map an httpx timeout to SandboxInfraError, not to a run timeout, and kill the command."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Answer like the server, but time out on ``/exec``.

        Args:
            request: The request.

        Returns:
            The answer.
        """
        seen.append(request.url.path)
        if request.url.path == "/exec":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True})

    async with SingularityEnvironment(_spec(), transport=httpx.MockTransport(handler)) as env:
        with pytest.raises(SandboxInfraError, match="past its"):
            await env.exec("sleep 1", timeout_sec=1)
    assert not issubclass(SandboxInfraError, TimeoutError)
    assert seen.index("/kill") == seen.index("/exec") + 1


async def test_a_cancelled_exec_kills_the_command(fake_state: Path, tmp_path: Path) -> None:
    """Kill the command's process group when the caller cancels ``exec``."""
    pid_file = tmp_path / "app" / "cmd.pid"
    async with SingularityEnvironment(_spec()) as env:
        task = asyncio.ensure_future(env.exec("echo $$ > cmd.pid; exec sleep 30"))
        await _wait_for_file(pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _wait_dead(int(pid_file.read_text()))
        assert (await env.exec("true")).ok


async def test_a_cancel_while_waiting_for_the_server_stops_the_container(fake_state: Path) -> None:
    """Stop the container and remove the staging files when the start is cancelled."""
    (fake_state / "idle").write_text("")
    env = SingularityEnvironment(_spec())
    task = asyncio.ensure_future(env.start())
    await _wait_for_file(fake_state / "exec.pid")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_dead(int((fake_state / "exec.pid").read_text()))
    assert env not in singularity_provider._LIVE
    assert env._staging is not None
    assert not env._staging.exists()


async def test_a_double_cancel_during_the_spawn_still_stops_the_container(
    fake_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the spawned container and stop it, even after two cancels during the spawn."""
    (fake_state / "idle").write_text("")
    image = tmp_path / "task.sif"
    image.write_bytes(b"SIF")
    gate = asyncio.Event()
    entered = asyncio.Event()
    spawned: list[asyncio.subprocess.Process] = []
    real_spawn = asyncio.create_subprocess_exec

    async def slow_spawn(*argv: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        """Hold the container spawn until the test opens the gate.

        Args:
            *argv: The program and its arguments.
            **kwargs: The spawn options.

        Returns:
            The spawned process.
        """
        entered.set()
        await gate.wait()
        process = await real_spawn(*argv, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(singularity_provider.asyncio, "create_subprocess_exec", slow_spawn)
    env = SingularityEnvironment(_spec(image=str(image)))
    task = asyncio.ensure_future(env.start())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(spawned) == 1
    assert spawned[0].returncode is not None
    assert env not in singularity_provider._LIVE


async def test_a_container_that_exits_early_raises_infra(fake_state: Path) -> None:
    """Raise SandboxInfraError with the log tail when the container exits before it is ready."""
    (fake_state / "exec_fail").write_text("")
    env = SingularityEnvironment(_spec())
    with pytest.raises(SandboxInfraError, match="fake exec_fail"):
        await env.start()
    assert env not in singularity_provider._LIVE


async def test_a_server_that_never_answers_times_out(fake_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise StartTimeoutError and stop the container when the server does not answer."""
    (fake_state / "idle").write_text("")
    monkeypatch.setattr(SingularityEnvironment, "ready_seconds", 0.3)
    env = SingularityEnvironment(_spec())
    with pytest.raises(StartTimeoutError):
        await env.start()
    assert await _wait_dead(int((fake_state / "exec.pid").read_text()))
    assert env not in singularity_provider._LIVE


async def test_start_twice_and_exec_before_start_are_refused(fake_state: Path) -> None:
    """Raise RuntimeError for a second start and for exec before start."""
    env = SingularityEnvironment(_spec())
    with pytest.raises(RuntimeError, match="not running"):
        await env.exec("true")
    async with env:
        with pytest.raises(RuntimeError, match="already started"):
            await env.start()
    with pytest.raises(RuntimeError, match="not running"):
        await env.exec("true")


async def test_the_server_stops_itself_after_the_lifetime(fake_state: Path) -> None:
    """End the container when the spec lifetime runs out, with no stop call."""
    async with SingularityEnvironment(_spec(lifetime_seconds=1)) as env:
        assert env._process is not None
        await asyncio.wait_for(env._process.wait(), 15)


async def test_stop_job_sandboxes_skips_other_jobs(fake_state: Path) -> None:
    """Return 0 for a job with no live sandbox."""
    async with SingularityEnvironment(_spec(tags={JOB_TAG: "job-a"})):
        assert await SingularityEnvironment.stop_job_sandboxes(f"job-{uuid.uuid4().hex}") == 0
