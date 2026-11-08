"""Tests for compose services in one box: parsing, size, start order and stop. No network."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from core.sandbox.capabilities import VERCEL_CAPABILITIES
from core.sandbox.environment import ExecResult, HealthcheckConfig, HealthcheckError, SandboxEnvironment
from core.sandbox.errors import SandboxInfraError
from core.sandbox.resources import ResourceMode, ResourceRequest
from core.sandbox.services import (
    ComposeError,
    ComposeService,
    ServicesTooLargeError,
    box_resources,
    healthcheck_command,
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


def test_yaml_booleans_become_lowercase_text() -> None:
    """Check that YAML booleans in a command and env read as compose writes them."""
    (service,) = parse_compose(
        "services:\n  s:\n    command: [app, --flag, true]\n    environment: {DEBUG: true, QUIET: false, N: 1}\n"
    )
    assert service.command == "app --flag true"
    assert service.environment == (("DEBUG", "true"), ("QUIET", "false"), ("N", "1"))


def test_yaml_1_1_scalars_stay_as_written() -> None:
    """Check that values YAML 1.1 would turn into booleans or numbers stay as compose (YAML 1.2) writes them."""
    (service,) = parse_compose(
        "services:\n  cache:\n    command: [redis-server, --appendonly, yes]\n"
        "    environment:\n      OFF: x\n      FEATURE: on\n      ZIP: 0123\n      VERSION: 1.10\n"
    )
    assert service.command == "redis-server --appendonly yes"
    assert service.environment == (("OFF", "x"), ("FEATURE", "on"), ("ZIP", "0123"), ("VERSION", "1.10"))


def test_null_environment_values_are_left_out() -> None:
    """Check that a null env value is left out, as in the list form, and an empty string stays empty."""
    (service,) = parse_compose(
        "services:\n  s:\n    command: x\n    environment:\n      E1:\n      E2: ~\n      E3: null\n      E4: ''\n"
    )
    assert service.environment == (("E4", ""),)


def test_interpolation_uses_an_empty_environment(tmp_path: Path) -> None:
    """Check that variables get their defaults or empty text, and that `$$` reaches bash as a literal `$`."""
    (db,) = parse_compose(
        "services:\n  db:\n"
        "    command: echo $$HOME \"$$HOME\" ${X:-d} '${X}|$X|${X:+y}' sh -c 'echo $$0'\n"
        "    working_dir: ${DIR:-/srv}\n"
        "    environment: {PASSWORD: '${PW:-postgres}', PRICE: 'a$$b'}\n"
        "    healthcheck: {test: [CMD-SHELL, pg_isready -U $$PASSWORD]}\n"
    )
    assert db.command == "echo '$HOME' '$HOME' d '||' sh -c 'echo $0'"
    assert db.working_dir == "/srv"
    assert db.environment == (("PASSWORD", "postgres"), ("PRICE", "a$b"))
    assert db.healthcheck is not None
    assert db.healthcheck.command == "pg_isready -U $PASSWORD"
    script = launch_command(db).split(" > ")[0].replace("setsid nohup ", "")
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
    )
    assert result.stdout.strip() == "$HOME $HOME d || sh -c echo $0"


def test_string_command_words_are_literal_as_in_compose(tmp_path: Path) -> None:
    """Check that a string command gets no shell expansion, because compose runs no shell."""
    (tmp_path / "a.txt").write_text("")
    (app,) = parse_compose("services:\n  app:\n    command: echo `id` *.txt {a,b} $$(pwd) ~ '&&' echo no\n")
    assert app.command == "echo '`id`' '*.txt' '{a,b}' '$(pwd)' '~' '&&' echo no"
    result = subprocess.run(
        ["bash", "-c", app.command],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.stdout == "`id` *.txt {a,b} $(pwd) ~ && echo no\n"
    with pytest.raises(ComposeError, match="command of compose service app is not valid shell words"):
        parse_compose("services:\n  app:\n    command: echo 'open\n")


@pytest.mark.parametrize(
    ("command", "operator"),
    [("a && b", "&"), ("a; b", ";"), ("a | b", "|"), ("a &", "&"), ("a > f", ">"), ("a < f", "<"), ("a 2>&1", ">")],
)
def test_unquoted_shell_operator_fails_the_parse(command: str, operator: str) -> None:
    """Check that an unquoted operator fails, because compose silently drops everything after it."""
    with pytest.raises(
        ComposeError, match=rf"unquoted '\{operator}'.*drops everything after an unquoted ;, &, \|, < or >.*sh -c"
    ):
        parse_compose(f"services:\n  app:\n    command: {command}\n")


@pytest.mark.parametrize(
    ("command", "words"),
    [
        ('sh -c "a && b"', ["sh", "-c", "a && b"]),
        ("echo 'x;y'", ["echo", "x;y"]),
        ("echo a\\;b", ["echo", "a;b"]),
        ("echo `a;b`", ["echo", "`a;b`"]),
        ("echo $$(a;b)", ["echo", "$(a;b)"]),
        ("echo `a b` $$(c 'd' e) z", ["echo", "`a b`", "$(c 'd' e)", "z"]),
        ("echo `a\\`b`", ["echo", "`a\\`b`"]),
        ('echo "q\\b" a\\>b', ["echo", "qb", "a>b"]),
        ("echo \\$$(a) \\\\x$$(b)", ["echo", "$(a)", "\\x$(b)"]),
    ],
)
def test_quoted_shell_operator_is_a_literal_word(command: str, words: list[str]) -> None:
    """Check that a quoted, escaped, backtick or $(...) operator stays inside its word, as in compose."""
    (app,) = parse_compose(f"services:\n  app:\n    command: {command}\n")
    assert shlex.split(app.command) == words


@pytest.mark.parametrize(
    ("command", "error"),
    [
        ("echo a(b", "unquoted '\\(' that does not open or close"),
        ("echo a)b", "unquoted '\\)' that does not open or close"),
        ("echo $$((1+2))", "unquoted '\\(' that does not open or close"),
        ("echo \\\\$$(a)", "unquoted '\\(' that does not open or close"),
        ("echo '\\'$$(a)", "unquoted '\\(' that does not open or close"),
        ("echo $$(a)\\\\$$(b)", "unquoted '\\(' that does not open or close"),
        ("echo `a", "is not closed"),
        ("echo $$(a", "is not closed"),
        ("echo a\\", "is not closed"),
    ],
)
def test_command_that_compose_rejects_fails_the_parse(command: str, error: str) -> None:
    """Check that a stray paren or an unclosed region fails, because compose rejects it too."""
    with pytest.raises(ComposeError, match=rf"command of compose service app is not valid shell words: .*{error}"):
        parse_compose(f"services:\n  app:\n    command: {command}\n")


def test_nested_interpolation_is_not_supported() -> None:
    """Check that a nested default fails with a clear not-supported message."""
    with pytest.raises(ComposeError, match=r"nested interpolation .* not supported"):
        parse_compose("services:\n  app:\n    command: echo ${X:-${Y:-deep}}\n")


def test_unclosed_interpolation_is_an_invalid_format() -> None:
    """Check that `${X` with no closing brace fails, as in compose."""
    with pytest.raises(ComposeError, match="invalid interpolation format"):
        parse_compose("services:\n  app:\n    command: echo ${X\n")


@pytest.mark.parametrize("form", ["${PW:?set the password}", "${PW?set the password}"])
def test_required_variable_names_the_variable(form: str) -> None:
    """Check that a required variable fails the parse, because the box has no host environment."""
    with pytest.raises(ComposeError, match="needs the variable PW"):
        parse_compose(f"services:\n  db:\n    command: postgres\n    environment: {{PW: '{form}'}}\n")


def test_depends_on_condition_the_box_cannot_wait_for_names_the_fix() -> None:
    """Check that service_completed_successfully fails loud, and the supported conditions still parse."""
    with pytest.raises(ComposeError, match=r"service_completed_successfully.*service_started and service_healthy"):
        parse_compose(
            "services:\n  api:\n    command: x\n"
            "    depends_on: {migrate: {condition: service_completed_successfully}}\n"
            "  migrate:\n    command: y\n"
        )
    services = parse_compose(
        "services:\n  api:\n    command: x\n"
        "    depends_on: {db: {condition: service_healthy}, cache: {condition: service_started}}\n"
        "  db:\n    command: y\n  cache:\n    command: z\n"
    )
    assert [service.name for service in services] == ["db", "cache", "api"]


def test_profile_services_are_skipped() -> None:
    """Check that a profiles service is left out before the command check and the size, and cannot be a dependency."""
    services = parse_compose(
        "services:\n  app:\n    command: x\n  debug:\n    image: busybox\n    profiles: [debug]\n    cpus: 8\n"
    )
    assert [service.name for service in services] == ["app"]
    alone = parse_compose("services:\n  app:\n    command: x\n")
    request = ResourceRequest(cpus=2)
    assert box_resources(request, services, VERCEL_CAPABILITIES) == box_resources(request, alone, VERCEL_CAPABILITIES)
    with pytest.raises(ComposeError, match="depends on debug, which sets 'profiles'"):
        parse_compose(
            "services:\n  app:\n    command: x\n    depends_on: [debug]\n"
            "  debug:\n    command: y\n    profiles: [debug]\n"
        )


def test_healthcheck_interval_zero_is_the_docker_default() -> None:
    """Check that a healthcheck interval of 0 means Docker's 30 s default, not a busy poll."""
    (db,) = parse_compose("services:\n  db:\n    command: x\n    healthcheck: {test: y, interval: 0s}\n")
    assert db.healthcheck is not None
    assert db.healthcheck.interval_sec == 30.0


def test_healthcheck_start_interval_zero_is_the_docker_default() -> None:
    """Check that a healthcheck start_interval of 0 means Docker's 5 s default, not a busy poll."""
    (db,) = parse_compose("services:\n  db:\n    command: x\n    healthcheck: {test: y, start_interval: 0s}\n")
    assert db.healthcheck is not None
    assert db.healthcheck.start_interval_sec == 5.0


def test_healthcheck_timeout_zero_is_the_docker_default() -> None:
    """Check that a healthcheck timeout of 0 means Docker's 30 s default, not no time."""
    for timeout in ("0s", "0"):
        (db,) = parse_compose(f"services:\n  db:\n    command: x\n    healthcheck: {{test: y, timeout: {timeout}}}\n")
        assert db.healthcheck is not None
        assert db.healthcheck.timeout_sec == 30.0


def test_healthcheck_reads_disable_and_plain_numbers_from_text() -> None:
    """Check that disable, a unitless interval and retries still parse when plain scalars stay text."""
    off, on = parse_compose(
        "services:\n  off:\n    command: x\n    healthcheck: {test: x, disable: true}\n"
        "  on:\n    command: y\n    healthcheck: {test: y, disable: false, interval: 5, retries: 3}\n"
    )
    assert off.healthcheck is None
    assert on.healthcheck is not None
    assert (on.healthcheck.interval_sec, on.healthcheck.retries) == (5.0, 3)


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
        ("services:\n  a:\n    command: [[x, y]]\n", "command of compose service a has an item"),
        ("services:\n  a:\n    entrypoint: [{x: y}]\n", "entrypoint of compose service a has an item"),
        ("services:\n  a:\n    command: x\n    environment: [{a: 1}]\n", "environment of compose service a"),
        ("services:\n  a:\n    command: x\n    environment: {A: [1]}\n", "environment of compose service a"),
        ("services:\n  a:\n    command: x\n    environment: {1: x}\n", "invalid environment name"),
        ("services:\n  a:\n    command: x\n    environment: {'A-B': 1}\n", "invalid environment name"),
        ("services:\n  a:\n    command: x\n    depends_on: [[b]]\n", "depends_on of compose service a"),
        ("services:\n  a:\n    command: x\n    healthcheck: {test: [CMD, [x]]}\n", "healthcheck test of"),
        ("services:\n  a:\n    command: x\n    deploy: fast\n", "deploy of compose service a is not a mapping"),
        ("services:\n  a:\n    command: x\n    deploy: [1]\n", "deploy of compose service a is not a mapping"),
        ("services:\n  a:\n    command: x\n    deploy: {resources: big}\n", "deploy.resources of"),
        ("services:\n  a:\n    command: x\n    deploy: {resources: {limits: 2}}\n", "deploy.resources.limits of"),
        ("services:\n  a:\n    command: x\n    healthcheck: yes-please\n", "healthcheck of compose service a"),
        ("services:\n  a:\n    command: x\n    healthcheck: {test: x, retries: many}\n", "healthcheck retries"),
        ("services:\n  a:\n    command: x\n    healthcheck: {test: x, retries: 1000000000}\n", "from 1 to 100"),
        ("services:\n  a:\n    command: x\n    healthcheck: {test: x, retries: 0}\n", "from 1 to 100"),
        ("services:\n  a:\n    command: x\n    healthcheck: {test: x, interval: 24h}\n", "outside 0 to 600"),
        ("services:\n  a:\n    command: x\n    healthcheck: {test: x, timeout: [1]}\n", "invalid duration"),
        ("services:\n  a:\n    command: x\n    working_dir: [a]\n", "working_dir that is not a string"),
    ],
)
def test_parse_rejects_what_the_box_cannot_run(text: str, message: str) -> None:
    """Check that each invalid compose file names its problem."""
    with pytest.raises(ComposeError, match=message):
        parse_compose(text)


def test_alias_bomb_is_refused_before_it_expands() -> None:
    """Check that nested YAML aliases in a command fail fast instead of growing a huge string."""
    lines = ["a0: &a0 [" + ",".join(["xxxxxxxx"] * 9) + "]"]
    lines += [f"a{i}: &a{i} [" + ",".join([f"*a{i - 1}"] * 9) + "]" for i in range(1, 7)]
    text = "\n".join([*lines, "services:\n  s:\n    command: [*a6]"])
    started = time.monotonic()
    with pytest.raises(ComposeError, match="has an item that is not a string or a number"):
        parse_compose(text)
    assert time.monotonic() - started < 1


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


async def test_healthcheck_runs_in_the_service_dir_and_env() -> None:
    """Check that the health poll gets the working_dir and env of its service on the command line."""
    (db,) = parse_compose(
        "services:\n  db:\n    command: postgres\n    working_dir: /srv/my db\n"
        "    environment: {POSTGRES_USER: app}\n"
        "    healthcheck: {test: [CMD-SHELL, pg_isready -U $$POSTGRES_USER], interval: 0s}\n"
    )
    env = FakeEnv()
    await start_services(env, (db,), [])
    checks = [command for command, _ in env.calls if "pg_isready" in command and "setsid" not in command]
    assert checks == [
        "cd '/srv/my db' && env POSTGRES_USER=app bash --noprofile --norc -c 'pg_isready -U $POSTGRES_USER'"
    ]


def test_healthcheck_without_dir_or_env_keeps_its_command() -> None:
    """Check that a service with no working_dir and no env runs its check text as is."""
    assert healthcheck_command(_service("a"), "true") == "true"


def test_healthcheck_command_runs_in_a_real_shell(tmp_path: Path) -> None:
    """Check in bash that the check sees the service directory and env value."""
    service = _service("a", working_dir=str(tmp_path), environment=(("MODE", "a b"),))
    result = subprocess.run(
        ["bash", "-c", healthcheck_command(service, 'echo "$(pwd)|$MODE"')],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.stdout.strip() == f"{tmp_path.resolve()}|a b"


async def test_start_deadline_ends_a_poll_on_a_gone_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a poll whose probes all raise ends at the start deadline."""

    class GoneEnv(FakeEnv):
        """Fail every probe as if the box were gone."""

        async def exec(self, command: str, **kwargs: Any) -> ExecResult:
            """Raise for each probe.

            Args:
                command: The shell command.
                kwargs: Unused.

            Returns:
                The launch result.

            Raises:
                SandboxInfraError: Each probe.
            """
            if "setsid" not in command:
                raise SandboxInfraError("gone")
            return await super().exec(command, **kwargs)

    monkeypatch.setattr("core.sandbox.services.SERVICES_START_TIMEOUT", 0.2)
    service = _service("db", healthcheck=HealthcheckConfig(command="true", interval_sec=0.01, retries=100))
    started = time.monotonic()
    with pytest.raises(HealthcheckError, match=r"db did not become healthy in 0\.2 seconds"):
        await start_services(GoneEnv(), (service,), [])
    assert time.monotonic() - started < 5


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
