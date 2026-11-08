"""Tests for the async Apple container sandbox provider.

A fake ``container`` script replaces the real CLI. It keeps each container
as a JSON record and a directory in ``tmp_path``, and runs ``container exec``
commands on this machine inside that directory. No test starts a VM.
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

import pytest

from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, SandboxEnvironment
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.factory import PROVIDERS, ProductionNotAllowedError, create_environment, load_provider
from core.sandbox.providers import apple_container as apple_provider
from core.sandbox.providers.apple_container import (
    AppleContainerEnvironment,
    _container_name,
    _job_label,
    _listed_containers,
)
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

IMAGE = "docker.io/library/python:3.12-slim"
STATE_ENV = "SKYNET_FAKE_CONTAINER_STATE"

FAKE_CONTAINER = textwrap.dedent(
    """
    import json, os, sys, time
    from pathlib import Path

    state = Path(os.environ["SKYNET_FAKE_CONTAINER_STATE"])
    boxes = state / "containers"
    boxes.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    with open(state / "calls.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps(args) + "\\n")
    verb = args[0] if args else ""

    def fail(name):
        path = state / name
        if path.exists():
            sys.stderr.write(path.read_text())
            sys.exit(1)

    def missing(name):
        if not (boxes / f"{name}.json").exists():
            sys.stderr.write(f"Error: notFound: container {name} not found\\n")
            sys.exit(1)

    if verb == "system" and args[1:2] == ["status"]:
        fail("status_fail")
        print("apiserver is running")
    elif verb == "image" and args[1:2] == ["pull"]:
        fail("pull_error")
        print(args[-1])
    elif verb == "run":
        fail("run_error")
        late = state / "run_late"
        if late.exists():
            # Model the client and API server split: the client hangs until
            # it is killed, and the server creates the container later.
            if os.fork():
                time.sleep(30)
                sys.exit(0)
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            delay = late.read_text()
            if delay == "never":
                sys.exit(0)
            time.sleep(float(delay))
        name = args[args.index("--name") + 1]
        labels = {}
        for index, arg in enumerate(args):
            if arg == "--label":
                key, value = args[index + 1].split("=", 1)
                labels[key] = value
        record = {"configuration": {"id": name, "labels": labels}, "status": "running", "args": args}
        (boxes / f"{name}.json").write_text(json.dumps(record))
        (state / "root" / name / "tmp").mkdir(parents=True, exist_ok=True)
        print(name)
    elif verb == "list":
        print(json.dumps([json.loads(path.read_text()) for path in sorted(boxes.glob("*.json"))]))
    elif verb == "stop":
        missing(args[-1])
        print(args[-1])
    elif verb == "delete":
        fail("delete_fail")
        missing(args[-1])
        (boxes / f"{args[-1]}.json").unlink()
        print(args[-1])
    elif verb == "exec":
        rest = args[1:]
        while rest and rest[0].startswith("--"):
            rest = rest[2:] if rest[0] == "--user" else rest[1:]
        name, command = rest[0], rest[1:]
        missing(name)
        fail("exec_error")
        root = state / "root" / name
        os.chdir(root)
        env = {"PATH": os.environ["PATH"], "TMPDIR": str(root / "tmp")}
        os.execvpe(command[0], command, env)
    else:
        sys.stderr.write(f"fake container: unknown call {args}\\n")
        sys.exit(2)
    """
)


@pytest.fixture
def container_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake ``container`` and point the provider at it.

    Args:
        tmp_path: The test's directory.
        monkeypatch: Sets the class defaults, the host platform and the fake's state directory.

    Returns:
        The fake's state directory.
    """
    state = tmp_path / "container-state"
    state.mkdir()
    container_bin = tmp_path / "container"
    container_bin.write_text(f"#!{sys.executable}\n{FAKE_CONTAINER}")
    container_bin.chmod(container_bin.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(STATE_ENV, str(state))
    monkeypatch.setattr(AppleContainerEnvironment, "default_container_bin", str(container_bin))
    monkeypatch.setattr(AppleContainerEnvironment, "poll_seconds", 0.0)
    monkeypatch.setattr(apple_provider.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(apple_provider.platform, "machine", lambda: "arm64")
    return state


def _calls(state: Path) -> list[list[str]]:
    """Read every ``container`` call that the fake logged.

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
    """Return the verb of every logged ``container`` call.

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


def _run_args(state: Path) -> list[str]:
    """Return the arguments of the one ``container run`` call.

    Args:
        state: The fake's state directory.

    Returns:
        The ``run`` arguments.
    """
    runs = [call for call in _calls(state) if call[0] == "run"]
    assert len(runs) == 1
    return runs[0]


@pytest.mark.usefixtures("container_state")
class TestAppleContainerEnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake ``container``."""

    provider_cls = AppleContainerEnvironment

    def make_spec(self, **overrides: Any) -> SandboxSpec:
        """Return the contract spec with the test image.

        Args:
            **overrides: Spec fields to replace.

        Returns:
            The spec.
        """
        return super().make_spec(**{"image": IMAGE, **overrides})

    async def test_upload_then_download_round_trips(self, sandbox: SandboxEnvironment) -> None:
        """Read back the exact text, through a path relative to the container's cwd.

        The fake runs on this machine, so ``/tmp`` would be the host's ``/tmp``.
        """
        await sandbox.upload_files({"contract/a.txt": "alpha\r\nבטא\n"})
        assert await sandbox.download_file("contract/a.txt") == "alpha\r\nבטא\n"
        assert (await sandbox.exec("cat contract/a.txt")).stdout == "alpha\r\nבטא\n"

    async def test_download_missing_file_returns_none(self, sandbox: SandboxEnvironment) -> None:
        """Return None for a relative path that does not exist."""
        assert await sandbox.download_file("contract/missing.txt") is None


def test_factory_registers_apple_container_for_development_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the provider with no extra and refuse it in production."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    entry = PROVIDERS["apple_container"]
    assert (entry.module, entry.cls, entry.extra, entry.production_allowed) == (
        "core.sandbox.providers.apple_container",
        "AppleContainerEnvironment",
        None,
        False,
    )
    assert load_provider("apple_container") is AppleContainerEnvironment
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        load_provider("apple_container")


def test_factory_creates_the_provider_with_its_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass provider kwargs through ``create_environment``."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    env = create_environment("apple_container", _spec(image=None), image=IMAGE, container_bin="/opt/container")
    assert isinstance(env, AppleContainerEnvironment)
    assert env._image == IMAGE
    assert env._bin == "/opt/container"


def test_preflight_passes_when_the_service_runs(container_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``container system status`` through the factory and return when it succeeds."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    factory.preflight("apple_container")
    assert _calls(container_state) == [["system", "status"]]


def test_preflight_fails_when_the_service_is_stopped(container_state: Path) -> None:
    """Raise SandboxFatalError when ``container system status`` fails."""
    (container_state / "status_fail").write_text("Error: apiserver is not running\n")
    with pytest.raises(SandboxFatalError, match="container system start"):
        AppleContainerEnvironment.preflight()


def test_preflight_fails_when_the_cli_is_missing(container_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise SandboxFatalError when no ``container`` is on PATH."""
    monkeypatch.setattr(AppleContainerEnvironment, "default_container_bin", str(container_state / "no-container"))
    with pytest.raises(SandboxFatalError, match="needs the container CLI"):
        AppleContainerEnvironment.preflight()


@pytest.mark.parametrize(("system", "machine"), [("Darwin", "x86_64"), ("Linux", "arm64"), ("Linux", "x86_64")])
def test_preflight_fails_off_apple_silicon(
    container_state: Path, monkeypatch: pytest.MonkeyPatch, system: str, machine: str
) -> None:
    """Raise SandboxFatalError on any host that is not macOS on arm64, before any CLI call."""
    monkeypatch.setattr(apple_provider.platform, "system", lambda: system)
    monkeypatch.setattr(apple_provider.platform, "machine", lambda: machine)
    with pytest.raises(SandboxFatalError, match="Apple silicon"):
        AppleContainerEnvironment.preflight()
    assert _calls(container_state) == []


def test_an_image_is_required() -> None:
    """Refuse a sandbox with no image: the provider builds none."""
    with pytest.raises(SandboxSpecError, match="needs an image"):
        AppleContainerEnvironment(_spec(image=None))


def test_a_disabled_network_is_refused() -> None:
    """Refuse a spec that asks for no egress: the provider only has open egress."""
    with pytest.raises(SandboxSpecError):
        AppleContainerEnvironment(_spec(network_disabled=True))


def test_the_spec_image_wins_over_the_provider_image() -> None:
    """Use the spec's image when both name one."""
    assert AppleContainerEnvironment(_spec(), image="other:1")._image == IMAGE


async def test_run_bounds_the_container_and_carries_the_job_label(container_state: Path) -> None:
    """Start a detached container with the labels, the image and an idle process bounded by the lifetime."""
    spec = _spec(lifetime_seconds=90.2, tags={JOB_TAG: "job-1"}, name="My Task")
    async with AppleContainerEnvironment(spec) as env:
        name = env.container_name
    args = _run_args(container_state)
    assert name is not None
    assert name.startswith("my-task-")
    assert args[:4] == ["run", "--detach", "--name", name]
    assert args[4:6] == ["--label", "skynet=sandbox"]
    assert args[6:8] == ["--label", "skynet.job=job-1"]
    image_at = args.index(IMAGE)
    assert args[image_at + 1 : image_at + 3] == ["sh", "-c"]
    assert args[-1] == "91"
    assert _verbs(container_state)[-2:] == ["stop", "delete"]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (ResourceMode.REQUEST, ["--cpus", "2", "--memory", "512M"]),
        (ResourceMode.AUTO, ["--cpus", "2", "--memory", "512M"]),
        (ResourceMode.LIMIT, ["--cpus", "2", "--memory", "512M"]),
        (ResourceMode.GUARANTEE, ["--cpus", "2", "--memory", "512M"]),
        (ResourceMode.IGNORE, []),
    ],
)
def test_resources_follow_the_resource_mode(mode: ResourceMode, expected: list[str]) -> None:
    """Size the VM in every mode but IGNORE, and never pass a disk size."""
    spec = _spec(resources=ResourceRequest(cpus=2, memory_mb=512, disk_mb=1024, mode=mode))
    args = AppleContainerEnvironment(spec)._run_args("box-1")
    image_at = args.index(IMAGE)
    assert args[6:image_at] == expected
    assert "1024" not in " ".join(args)


async def test_env_values_stay_off_the_command_line(container_state: Path) -> None:
    """Send spec and exec env values on stdin only."""
    async with AppleContainerEnvironment(_spec(env={"SPEC_SECRET": "spec-value-123"})) as env:
        result = await env.exec('printf "%s %s" "$SPEC_SECRET" "$EXEC_SECRET"', env={"EXEC_SECRET": "exec-value-456"})
    assert result.stdout == "spec-value-123 exec-value-456"
    logged = json.dumps(_calls(container_state))
    for value in ("spec-value-123", "exec-value-456"):
        assert value not in logged


async def test_exec_runs_in_the_cwd(container_state: Path) -> None:
    """Change into the working directory before the command runs."""
    async with AppleContainerEnvironment(_spec()) as env:
        await env.upload_files({"work dir/x.txt": "x"})
        result = await env.exec("cat x.txt", cwd="work dir")
    assert result.stdout == "x"


async def test_sudo_runs_the_command_as_root(container_state: Path) -> None:
    """Pass ``--user root`` only for a sudo command."""
    async with AppleContainerEnvironment(_spec()) as env:
        await env.exec("true", sudo=True)
        await env.exec("echo plain")
    commands = [call for call in _calls(container_state) if call[0] == "exec" and "--interactive" in call]
    assert commands[0][2:4] == ["--user", "root"]
    assert "--user" not in commands[1]


async def test_exec_timeout_kills_the_command_and_reports_timed_out(container_state: Path) -> None:
    """Kill the command at its limit and return ``timed_out=True``."""
    async with AppleContainerEnvironment(_spec()) as env:
        started = time.monotonic()
        result = await env.exec("sleep 30", timeout_sec=1)
        elapsed = time.monotonic() - started
        assert result.timed_out
        assert result.exit_code == 124
        assert elapsed < 10
        plain = await env.exec("exit 124", timeout_sec=5)
        assert plain.exit_code == 124
        assert not plain.timed_out


def _alive(pid: int) -> bool:
    """Say whether a process still runs.

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


async def test_a_cancelled_exec_kills_the_command(container_state: Path) -> None:
    """Kill the command's process group when the caller cancels the exec."""
    async with AppleContainerEnvironment(_spec()) as env:
        pid_file = container_state / "root" / str(env.container_name) / "pid"
        task = asyncio.create_task(env.exec("echo $$ > pid; exec sleep 30"))
        for _ in range(200):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        pid = int(pid_file.read_text())
        assert _alive(pid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if not _alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not _alive(pid)
        assert (await env.exec("echo still")).stdout.strip() == "still"


async def test_exec_before_start_raises(container_state: Path) -> None:
    """Refuse to run a command before start."""
    with pytest.raises(RuntimeError, match="not running"):
        await AppleContainerEnvironment(_spec()).exec("true")


async def test_upload_refuses_an_empty_path(container_state: Path) -> None:
    """Refuse an empty sandbox path."""
    async with AppleContainerEnvironment(_spec()) as env:
        with pytest.raises(SandboxSpecError):
            await env.upload_files({"": "x"})


@pytest.mark.parametrize(
    "stderr",
    [
        "Error: XPC connection error: Connection invalid\n",
        "Error: failed to connect to the apiserver. Ensure it is running with container system start\n",
    ],
)
async def test_a_stopped_service_is_fatal(container_state: Path, stderr: str) -> None:
    """Raise SandboxFatalError once, with no retry, when the system service is down."""
    (container_state / "run_error").write_text(stderr)
    with pytest.raises(SandboxFatalError, match="container system start"):
        async with AppleContainerEnvironment(_spec()):
            pass
    assert _verbs(container_state).count("run") == 1


async def test_start_pulls_the_image_with_a_long_deadline_before_run(
    container_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pull the image first, under the long pull deadline, so the run deadline covers only the boot."""
    deadlines: dict[str, float] = {}
    real_cli = apple_provider._cli

    async def spy(container_bin: str, args: list[str], **kwargs: Any) -> Any:
        """Record the deadline of each verb, then run the fake CLI."""
        deadlines.setdefault(args[0], kwargs.get("cli_seconds", apple_provider._CLI_TIMEOUT_SECONDS))
        return await real_cli(container_bin, args, **kwargs)

    monkeypatch.setattr(apple_provider, "_cli", spy)
    async with AppleContainerEnvironment(_spec()):
        pass
    calls = _calls(container_state)
    assert calls[0] == ["image", "pull", "--progress", "none", IMAGE]
    assert calls[1][0] == "run"
    assert deadlines["image"] == apple_provider._PULL_TIMEOUT_SECONDS
    assert deadlines["run"] == apple_provider._CLI_TIMEOUT_SECONDS


async def test_a_failed_pull_raises_before_any_run(container_state: Path) -> None:
    """Raise SandboxInfraError for a failed pull and create no container."""
    (container_state / "pull_error").write_text("Error: failed to pull image: manifest unknown\n")
    with pytest.raises(SandboxInfraError, match="could not pull the image"):
        async with AppleContainerEnvironment(_spec()):
            pass
    assert _verbs(container_state) == ["image"]


async def test_a_container_that_a_killed_run_creates_late_is_removed(
    container_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove the container that the server creates after the run client was killed at its deadline."""
    (container_state / "run_late").write_text("1.5")
    real_cli = apple_provider._cli

    async def short_run(container_bin: str, args: list[str], **kwargs: Any) -> Any:
        """Give ``run`` a 0.5 s deadline and leave every other call alone."""
        if args[0] == "run":
            kwargs["cli_seconds"] = 0.5
        return await real_cli(container_bin, args, **kwargs)

    monkeypatch.setattr(apple_provider, "_cli", short_run)
    monkeypatch.setattr(AppleContainerEnvironment, "late_run_seconds", 10.0)
    monkeypatch.setattr(apple_provider, "_LATE_POLL_SECONDS", 0.1)
    with pytest.raises(SandboxInfraError, match="did not finish"):
        async with AppleContainerEnvironment(_spec()):
            pass
    verbs = _verbs(container_state)
    assert "list" in verbs[verbs.index("run") :]
    assert list((container_state / "containers").glob("*.json")) == []


async def test_a_killed_run_gives_up_when_no_container_appears(
    container_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop the late-container search after ``late_run_seconds`` when the server creates nothing."""
    (container_state / "run_late").write_text("never")
    real_cli = apple_provider._cli

    async def short_run(container_bin: str, args: list[str], **kwargs: Any) -> Any:
        """Give ``run`` a 0.5 s deadline and leave every other call alone."""
        if args[0] == "run":
            kwargs["cli_seconds"] = 0.5
        return await real_cli(container_bin, args, **kwargs)

    monkeypatch.setattr(apple_provider, "_cli", short_run)
    monkeypatch.setattr(AppleContainerEnvironment, "late_run_seconds", 0.3)
    monkeypatch.setattr(apple_provider, "_LATE_POLL_SECONDS", 0.1)
    started = time.monotonic()
    with pytest.raises(SandboxInfraError, match="did not finish"):
        async with AppleContainerEnvironment(_spec()):
            pass
    assert time.monotonic() - started < 5
    assert _verbs(container_state).count("list") >= 2


async def test_other_run_failures_are_infra_errors_without_a_retry(container_state: Path) -> None:
    """Raise SandboxInfraError for a refusal that is not about the service, after one run."""
    (container_state / "run_error").write_text("Error: failed to pull image: manifest unknown\n")
    with pytest.raises(SandboxInfraError, match="manifest unknown"):
        async with AppleContainerEnvironment(_spec()):
            pass
    verbs = _verbs(container_state)
    assert verbs.count("run") == 1
    assert verbs[-1] == "delete"


async def test_a_container_that_never_answers_times_out(container_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise StartTimeoutError when no command runs in time, then remove the container."""
    monkeypatch.setattr(AppleContainerEnvironment, "ready_seconds", 0.2)
    monkeypatch.setattr(AppleContainerEnvironment, "poll_seconds", 0.05)
    (container_state / "exec_error").write_text("Error: container is not running\n")
    with pytest.raises(StartTimeoutError):
        async with AppleContainerEnvironment(_spec()):
            pass
    verbs = _verbs(container_state)
    assert verbs.count("run") == 1
    assert verbs[-2:] == ["stop", "delete"]


async def test_start_twice_raises(container_state: Path) -> None:
    """Refuse a second start of the same sandbox."""
    async with AppleContainerEnvironment(_spec()) as env:
        with pytest.raises(RuntimeError, match="already started"):
            await env.start()


async def test_stop_deletes_once_and_logs_a_failure(container_state: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Delete the container once; log a failed delete and do not raise."""
    env = AppleContainerEnvironment(_spec())
    await env.start()
    (container_state / "delete_fail").write_text("Error: connection refused\n")
    await env.stop()
    await env.stop()
    assert _verbs(container_state).count("delete") == 1
    assert "connection refused" in caplog.text


async def test_stop_of_a_gone_container_succeeds(container_state: Path) -> None:
    """Treat a container that is already gone as removed."""
    assert await apple_provider._remove_container(AppleContainerEnvironment.default_container_bin, "never-existed")


async def test_stop_job_sandboxes_continues_after_a_failure(
    container_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Count only the confirmed removals, try every container of the job, and skip other containers."""
    job_id = f"job-{uuid.uuid4().hex}"
    envs = [AppleContainerEnvironment(_spec(tags={JOB_TAG: job_id})) for _ in range(3)]
    other = AppleContainerEnvironment(_spec(tags={JOB_TAG: "other-job"}))
    for env in [*envs, other]:
        await env.start()
    failing = envs[0].container_name
    real_remove = apple_provider._remove_container

    async def flaky_remove(container_bin: str, name: str) -> bool:
        """Fail the removal of the first container only."""
        if name == failing:
            return False
        return await real_remove(container_bin, name)

    monkeypatch.setattr(apple_provider, "_remove_container", flaky_remove)
    assert await AppleContainerEnvironment.stop_job_sandboxes(job_id) == 2
    assert _verbs(container_state).count("delete") == 2
    remaining = {path.stem for path in (container_state / "containers").glob("*.json")}
    assert remaining == {failing, other.container_name}
    monkeypatch.undo()
    for env in [*envs, other]:
        await env.stop()


async def test_stop_job_sandboxes_logs_a_failed_list(container_state: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Return 0 and log when the CLI cannot list the containers."""
    missing = str(container_state / "no-container")
    assert await AppleContainerEnvironment.stop_job_sandboxes("j", container_bin=missing) == 0
    assert "Could not list" in caplog.text


def test_listed_containers_read_both_snapshot_shapes() -> None:
    """Read the id and labels at the top level or under ``configuration``, and skip bad rows."""
    payload = json.dumps(
        [
            {"configuration": {"id": "a", "labels": {"skynet": "sandbox"}}},
            {"id": "b", "labels": {}},
            {"configuration": {"id": "c"}},
            "junk",
        ]
    ).encode()
    assert _listed_containers(payload) == [("a", {"skynet": "sandbox"}), ("b", {})]
    with pytest.raises(TypeError):
        _listed_containers(b'{"not": "a list"}')
    with pytest.raises(ValueError):
        _listed_containers(b"not json")


def test_container_names_are_safe() -> None:
    """Turn any stem into a short ``[a-z0-9-]`` name with a random suffix."""
    name = _container_name("Hello_World!!" + "x" * 80)
    assert name.startswith("hello-world-")
    assert len(name) <= 59
    assert all(char in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in name)
    assert _container_name(None).startswith("skynet-")
    assert _container_name("***").startswith("skynet-")
    assert _container_name("a") != _container_name("a")


def test_job_labels_are_plain_tokens() -> None:
    """Keep a plain job id, and hash one that a label argument should not carry."""
    assert _job_label("job-1") == "job-1"
    hashed = _job_label("job/with spaces")
    assert hashed.startswith("sha256-")
    assert len(hashed) == 63
    assert _job_label("x" * 64) != _job_label("x" * 65)
    assert _job_label("job/with spaces") == hashed
