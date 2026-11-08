"""Tests for compose services in one box: parsing, size, start order and stop. No network."""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from core.sandbox.capabilities import VERCEL_CAPABILITIES
from core.sandbox.environment import ExecResult, HealthcheckError, SandboxEnvironment
from core.sandbox.errors import SandboxInfraError
from core.sandbox.resources import ResourceMode, ResourceRequest
from core.sandbox.services import (
    ComposeError,
    ComposeService,
    ServicesTooLargeError,
    box_resources,
    launch_command,
    parse_compose,
    start_services,
    stop_command,
    stop_services,
)

COMPOSE = """
services:
  app:
    command: python app.py
    depends_on: [cache, db]
    working_dir: /srv
    environment:
      MODE: test
  db:
    entrypoint: [postgres]
    command: ["-D", "/data dir"]
    mem_limit: 1g
    cpus: 1
    healthcheck:
      test: ["CMD", "pg_isready", "-q"]
      interval: 1s
      retries: 5
  cache:
    command: redis-server
    environment: ["A=1", "PASSTHROUGH"]
    deploy:
      resources:
        limits:
          cpus: "0.25"
          memory: 256M
"""


class FakeEnv:
    """Run box commands against a list of planned exit codes and log them."""

    healthcheck = SandboxEnvironment.healthcheck

    def __init__(self, codes: dict[str, list[int]] | None = None) -> None:
        """Keep the planned exit codes.

        Args:
            codes: For a command substring, the exit codes it returns in turn.
        """
        self.codes = codes or {}
        self.calls: list[tuple[str, str | None]] = []

    async def exec(self, command: str, *, cwd: str | None = None, **kwargs: Any) -> ExecResult:
        """Log one command and return its next planned exit code.

        Args:
            command: The shell command.
            cwd: The working directory.
            kwargs: Unused.

        Returns:
            The result; exit code 0 when nothing is planned.
        """
        self.calls.append((command, cwd))
        for part, codes in self.codes.items():
            if part in command and codes:
                return ExecResult(exit_code=codes.pop(0))
        return ExecResult(exit_code=0)


def _service(name: str, **fields: Any) -> ComposeService:
    """Make a service with a sleep command.

    Args:
        name: The service name.
        fields: Other service fields.

    Returns:
        The service.
    """
    return ComposeService(name=name, command="sleep 30", **fields)


def test_parse_reads_command_env_size_and_start_order() -> None:
    """Check each compose field and that dependencies start first."""
    services = parse_compose(COMPOSE)
    assert [service.name for service in services] == ["db", "cache", "app"]
    db, cache, app = services
    assert db.command == "postgres -D '/data dir'"
    assert (db.cpus, db.memory_mb) == (1.0, 1024)
    assert db.healthcheck is not None
    assert (db.healthcheck.command, db.healthcheck.interval_sec, db.healthcheck.retries) == ("pg_isready -q", 1.0, 5)
    assert (cache.cpus, cache.memory_mb, cache.environment) == (0.25, 256, (("A", "1"),))
    assert (app.cpus, app.memory_mb, app.working_dir, app.environment) == (0.5, 512, "/srv", (("MODE", "test"),))
    assert app.healthcheck is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("services: [a]", "'services' mapping"),
        ("services:\n  a:\n    image: redis\n", "no container runtime"),
        ("services:\n  a:\n    command: x\n    depends_on: [b]\n", "unknown services: b"),
        ("services:\n  a:\n    command: x\n    depends_on: [b]\n  b:\n    command: y\n    depends_on: [a]\n", "cycle"),
        ("services:\n  a:\n    command: x\n    mem_limit: lots\n", "invalid memory"),
        ("services:\n  a:\n    command: x\n    cpus: -1\n", "invalid cpus"),
        ("services:\n  a b:\n    command: x\n", "must use only"),
        ("services:\n  a:\n    command: x\n    environment: {'1X': y}\n", "invalid environment name"),
        ("services: {a: [", "not valid YAML"),
    ],
)
def test_parse_rejects_what_the_box_cannot_run(text: str, message: str) -> None:
    """Check that each invalid compose file names its problem."""
    with pytest.raises(ComposeError, match=message):
        parse_compose(text)


def test_disabled_healthcheck_is_no_check() -> None:
    """Check the compose forms that turn the healthcheck off."""
    text = (
        "services:\n  a:\n    command: x\n    healthcheck: {disable: true}\n"
        "  b:\n    command: y\n    healthcheck: {test: [NONE]}\n"
    )
    assert [service.healthcheck for service in parse_compose(text)] == [None, None]


def test_no_services_keep_the_request() -> None:
    """Check that a box without services keeps its exact size."""
    request = ResourceRequest(cpus=2)
    assert box_resources(request, (), VERCEL_CAPABILITIES) is request


def test_box_grows_by_each_service_cpu_and_memory() -> None:
    """Check the vCPU count covers the workload plus every service, rounded to the Vercel rules."""
    services = parse_compose(COMPOSE)
    grown = box_resources(ResourceRequest(cpus=2), services, VERCEL_CAPABILITIES)
    # 2 + 1 + 0.25 + 0.5 vCPUs and 4096 + 1024 + 256 + 512 MB need 3 vCPUs, so an even 4.
    assert (grown.cpus, grown.memory_mb, grown.mode) == (4, 8192, ResourceMode.REQUEST)


def test_memory_alone_can_grow_the_box() -> None:
    """Check that memory-heavy services add vCPUs, because Vercel sizes memory by vCPU."""
    services = (_service("db", cpus=0.1, memory_mb=6000),)
    grown = box_resources(ResourceRequest(cpus=1), services, VERCEL_CAPABILITIES)
    assert grown.cpus == 4


def test_ignore_mode_box_gets_a_size_for_its_services() -> None:
    """Check that services give an unsized box a real size, so the reservation prices them."""
    grown = box_resources(ResourceRequest(cpus=1, mode=ResourceMode.IGNORE), (_service("a"),), VERCEL_CAPABILITIES)
    assert (grown.cpus, grown.mode) == (2, ResourceMode.REQUEST)


def test_services_too_large_name_the_fix() -> None:
    """Check that services past the largest box fail with the fix in the message."""
    services = (_service("big", cpus=31),)
    with pytest.raises(
        ServicesTooLargeError, match=r"largest vercel box has 32 vCPUs.*lower its 'cpus' or 'mem_limit'"
    ):
        box_resources(ResourceRequest(cpus=2), services, VERCEL_CAPABILITIES)


async def test_services_start_in_order_and_stop_in_reverse() -> None:
    """Check the launch order, the working directory, the health poll and the reverse stop."""
    services = parse_compose(COMPOSE)
    env = FakeEnv({"pg_isready": [1, 0]})
    started: list[ComposeService] = []
    await start_services(env, services, started)
    launches = [(command, cwd) for command, cwd in env.calls if "setsid" in command]
    assert [cwd for _, cwd in launches] == [None, None, "/srv"]
    assert [service.name for service in started] == ["db", "cache", "app"]
    assert ["svc-db.pid" in command for command, _ in launches] == [True, False, False]
    # db launches, then polls twice before cache launches.
    assert ["pg_isready" in command for command, _ in env.calls[:4]] == [False, True, True, False]
    env.calls.clear()
    await stop_services(env, started)
    assert [command.split(".svc-")[1].split(".pid")[0] for command, _ in env.calls] == ["app", "cache", "db"]
    assert started == []


async def test_failed_launch_names_the_service_and_keeps_it_for_the_stop() -> None:
    """Check that a launch failure stops the start and the service is still stopped later."""
    env = FakeEnv({"svc-cache.pid": [7]})
    started: list[ComposeService] = []
    with pytest.raises(SandboxInfraError, match="cache did not start: exit code 7"):
        await start_services(env, parse_compose(COMPOSE), started)
    assert [service.name for service in started] == ["db", "cache"]


async def test_unhealthy_service_names_its_log() -> None:
    """Check that a failed health poll names the service and its log path."""
    services = parse_compose(COMPOSE)
    env = FakeEnv({"pg_isready": [1] * 5})
    db = services[0]
    fast = ComposeService(
        **{**db.__dict__, "healthcheck": type(db.healthcheck)(command="pg_isready -q", interval_sec=0, retries=2)}
    )
    with pytest.raises(HealthcheckError, match=r"db is not healthy\. Read /tmp/\.svc-db\.log"):
        await start_services(env, (fast,), [])


async def test_failed_stop_still_stops_the_rest() -> None:
    """Check that one failed stop is logged and the next service still stops."""

    class BrokenEnv(FakeEnv):
        """Fail the stop of one service."""

        async def exec(self, command: str, **kwargs: Any) -> ExecResult:
            """Raise for the cache stop.

            Args:
                command: The shell command.
                kwargs: Unused.

            Returns:
                The result.

            Raises:
                SandboxInfraError: The cache stop.
            """
            if "svc-cache" in command:
                raise SandboxInfraError("gone")
            return await super().exec(command, **kwargs)

    env = BrokenEnv()
    started = [_service("db"), _service("cache"), _service("app")]
    await stop_services(env, started)
    assert [command.split(".svc-")[1].split(".pid")[0] for command, _ in env.calls] == ["app", "db"]


def test_launch_keeps_env_values_out_of_the_exec_env() -> None:
    """Check that service env values go on the command line, quoted."""
    command = launch_command(_service("a", environment=(("MODE", "a b"),)))
    assert command.startswith("env 'MODE=a b' setsid nohup bash --noprofile --norc -c 'sleep 30'")


def _running(pid: int) -> bool:
    """Tell whether a Linux process runs, so a zombie that nobody reaped counts as ended.

    Args:
        pid: The process id.

    Returns:
        True when the process exists and is not a zombie.
    """
    stat = Path(f"/proc/{pid}/stat")
    return stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"


@pytest.mark.skipif(shutil.which("setsid") is None or not Path("/proc").is_dir(), reason="the box runs Linux")
def test_launch_and_stop_commands_run_in_a_real_shell() -> None:
    """Check in bash that the launch leaves the service running and the stop ends it."""
    service = ComposeService(name=f"t-{uuid.uuid4().hex[:8]}", command="sleep 60")
    subprocess.run(["bash", "-c", launch_command(service)], check=True, timeout=10)
    try:
        pid = int(Path(service.pid_path).read_text())
        assert _running(pid)
        subprocess.run(["bash", "-c", stop_command(service)], check=True, timeout=10)
        deadline = time.monotonic() + 5
        while _running(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _running(pid)
        assert not Path(service.pid_path).exists()
    finally:
        subprocess.run(["bash", "-c", stop_command(service)], check=False, timeout=10)
