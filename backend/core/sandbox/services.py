"""Compose services that run as processes inside one sandbox box (SBX-26).

A Vercel box has no container runtime, so a compose ``image`` is not pulled.
Each service runs its ``entrypoint`` and ``command`` as a background process
in the box, in ``depends_on`` order. The box grows by the declared ``cpus``
and ``mem_limit`` of each service, so the reservation prices the services
too. Services start after the box (or its snapshot restore) is up, and they
stop before a snapshot, so a snapshot never holds a running service.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from .capabilities import EnvironmentCapabilities, SandboxSpecError
from .environment import HealthcheckConfig, HealthcheckError, SandboxEnvironment
from .errors import SandboxInfraError
from .resources import ResourceMode, ResourceRequest

logger = logging.getLogger(__name__)

# A service that declares no size still uses the box; these are the shares it gets.
DEFAULT_SERVICE_CPUS = 0.5
DEFAULT_SERVICE_MEMORY_MB = 512
LAUNCH_TIMEOUT = 30.0
SERVICE_STOP_TIMEOUT = 10.0
SERVICE_PREFIX = "/tmp/.svc-"
# A compose file can set any poll timing; these limits keep one setup from
# waiting for hours. The start deadline covers every service of the box.
MAX_HEALTHCHECK_RETRIES = 100
MAX_HEALTHCHECK_DURATION = 600.0
SERVICES_START_TIMEOUT = 900.0

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BYTES = re.compile(r"^(\d+(?:\.\d+)?)\s*([bkmg]?)b?$")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(us|ms|s|m|h)")
_UNIT_BYTES = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_UNIT_SECONDS = {"us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")
_INTEGER = re.compile(r"^\d+$")
_TRUE = ("true", "True", "TRUE")
_KEPT_TAGS = ("tag:yaml.org,2002:null", "tag:yaml.org,2002:merge")


class _ComposeLoader(yaml.SafeLoader):
    """Load YAML with every plain scalar as text, except null and the merge key."""


# PyYAML resolves YAML 1.1 types: `yes` and `on` become booleans, `0123` an
# octal int and `1.10` the float 1.1. Compose (YAML 1.2) keeps these as
# written, so plain scalars stay text and only the fields that need a number
# parse one.
_ComposeLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag in _KEPT_TAGS]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class ComposeError(SandboxSpecError):
    """Signal a compose file that the box cannot run."""


class ServicesTooLargeError(SandboxSpecError):
    """Signal compose services that do not fit in the largest box."""


@dataclass(frozen=True)
class ComposeService:
    """Hold one compose service as a box process."""

    name: str
    command: str
    environment: tuple[tuple[str, str], ...] = ()
    working_dir: str | None = None
    cpus: float = DEFAULT_SERVICE_CPUS
    memory_mb: int = DEFAULT_SERVICE_MEMORY_MB
    depends_on: tuple[str, ...] = ()
    healthcheck: HealthcheckConfig | None = None

    @property
    def pid_path(self) -> str:
        """Return the box path of the service process id."""
        return f"{SERVICE_PREFIX}{self.name}.pid"

    @property
    def log_path(self) -> str:
        """Return the box path of the service output."""
        return f"{SERVICE_PREFIX}{self.name}.log"


def parse_compose(text: str) -> tuple[ComposeService, ...]:
    """Read a compose file into services in start order.

    Args:
        text: The compose YAML text.

    Returns:
        The services, each after the services it depends on. Services with
        no order between them keep their file order.

    Raises:
        ComposeError: The file is not valid compose, a service has no
            command, or ``depends_on`` names an unknown service or a cycle.
    """
    try:
        data = yaml.load(text, Loader=_ComposeLoader)
    except yaml.YAMLError as error:
        raise ComposeError(f"The compose file is not valid YAML: {error}") from error
    if not isinstance(data, Mapping) or not isinstance(data.get("services"), Mapping):
        raise ComposeError("The compose file needs a 'services' mapping.")
    services = [_service(str(name), body) for name, body in data["services"].items()]
    return _start_order(services)


def _service(name: str, body: Any) -> ComposeService:
    """Read one compose service.

    Args:
        name: The service name.
        body: The service mapping.

    Returns:
        The service.

    Raises:
        ComposeError: The service is invalid or has no command.
    """
    if not _NAME.match(name):
        raise ComposeError(f"The compose service name {name!r} must use only letters, digits, '.', '_' and '-'.")
    if not isinstance(body, Mapping):
        raise ComposeError(f"The compose service {name} must be a mapping.")
    parts = [_command_text(name, key, body.get(key)) for key in ("entrypoint", "command") if body.get(key) is not None]
    if not any(parts):
        # The box has no container runtime, so an image-only service has nothing to run.
        raise ComposeError(
            f"The compose service {name} has no command. The box has no container runtime: "
            "add a 'command' or 'entrypoint' that runs in the box."
        )
    limits = _mapping(name, "deploy.resources.limits", _deploy_limits(name, body.get("deploy")))
    cpus = body.get("cpus", limits.get("cpus"))
    memory = body.get("mem_limit", limits.get("memory"))
    working_dir = body.get("working_dir")
    if working_dir is not None and not isinstance(working_dir, str):
        raise ComposeError(f"The compose service {name} has a working_dir that is not a string.")
    return ComposeService(
        name=name,
        command=" ".join(part for part in parts if part),
        environment=_environment(name, body.get("environment")),
        working_dir=working_dir or None,
        cpus=_cpus(name, cpus) if cpus is not None else DEFAULT_SERVICE_CPUS,
        memory_mb=_memory_mb(name, memory) if memory is not None else DEFAULT_SERVICE_MEMORY_MB,
        depends_on=_depends_on(name, body.get("depends_on")),
        healthcheck=_healthcheck(name, body.get("healthcheck")),
    )


def _mapping(name: str, field: str, value: Any) -> Mapping[Any, Any]:
    """Read an optional compose mapping field.

    Args:
        name: The service name, for the error.
        field: The field name, for the error.
        value: The field value, or ``None``.

    Returns:
        The mapping, or an empty mapping when the field is not set.

    Raises:
        ComposeError: The value is not a mapping.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ComposeError(f"The {field} of compose service {name} is not a mapping.")
    return value


def _deploy_limits(name: str, deploy: Any) -> Any:
    """Return the ``deploy.resources.limits`` value of a service.

    Args:
        name: The service name, for the error.
        deploy: The ``deploy`` value, or ``None``.

    Returns:
        The ``limits`` value, or ``None`` when it is not set.

    Raises:
        ComposeError: ``deploy`` or ``resources`` is not a mapping.
    """
    resources = _mapping(name, "deploy", deploy).get("resources")
    return _mapping(name, "deploy.resources", resources).get("limits")


def _scalar(name: str, field: str, value: Any) -> str:
    """Turn one compose scalar into text.

    Only plain scalars pass, so YAML aliases cannot expand a nested list into
    a huge string. The loader keeps plain scalars as text; a number or a
    boolean comes only from an explicit tag, and a boolean becomes lowercase.

    Args:
        name: The service name, for the error.
        field: The field name, for the error.
        value: The value.

    Returns:
        The value as text.

    Raises:
        ComposeError: The value is not a string or a number.
    """
    # bool is an int subclass, so it must come first or !!bool true becomes "True".
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str | int | float):
        raise ComposeError(f"The {field} of compose service {name} has an item that is not a string or a number.")
    return str(value)


def _command_text(name: str, field: str, value: Any) -> str:
    """Turn a compose ``command`` or ``entrypoint`` into shell text.

    Args:
        name: The service name, for the error.
        field: The field name, for the error.
        value: A string, or a list of scalar arguments.

    Returns:
        The shell text.

    Raises:
        ComposeError: The value is neither a string nor a list of scalars.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return shlex.join(_scalar(name, field, item) for item in value)
    raise ComposeError(f"The {field} of compose service {name} is not a string or a list.")


def _environment(name: str, value: Any) -> tuple[tuple[str, str], ...]:
    """Read the compose ``environment`` of a service.

    Args:
        name: The service name, for the error.
        value: A mapping, a list of ``KEY=VALUE`` items, or ``None``.

    Returns:
        The variables in file order. A list item with no value is left out,
        because the box has no host environment to pass through.

    Raises:
        ComposeError: A key is not a valid variable name, or a value is not
            a scalar.
    """
    if value is None:
        return ()
    if isinstance(value, Mapping):
        pairs = [
            (_scalar(name, "environment", key), "" if item is None else _scalar(name, "environment", item))
            for key, item in value.items()
        ]
    elif isinstance(value, list):
        items = [_scalar(name, "environment", item) for item in value]
        pairs = [tuple(item.split("=", 1)) for item in items if "=" in item]
    else:
        raise ComposeError(f"The compose service {name} has an environment that is not a mapping or a list.")
    for key, _ in pairs:
        if not _ENV_KEY.match(key):
            raise ComposeError(f"The compose service {name} has an invalid environment name {key!r}.")
    return tuple((key, item) for key, item in pairs)


def _cpus(name: str, value: Any) -> float:
    """Read a compose CPU count.

    Args:
        name: The service name, for the error.
        value: A number or a numeric string.

    Returns:
        The CPU count.

    Raises:
        ComposeError: The value is not a positive number.
    """
    try:
        cpus = float(value)
    except (TypeError, ValueError):
        cpus = math.nan
    if isinstance(value, bool) or not math.isfinite(cpus) or cpus <= 0:
        raise ComposeError(f"The compose service {name} has an invalid cpus value {value!r}.")
    return cpus


def _memory_mb(name: str, value: Any) -> int:
    """Read a compose memory size in MB.

    Args:
        name: The service name, for the error.
        value: Bytes as an int, or a string such as ``512m`` or ``1g``.

    Returns:
        The size in MB (1024 * 1024 bytes), rounded up.

    Raises:
        ComposeError: The value is not a positive size.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        size = float(value)
    else:
        match = _BYTES.match(str(value).strip().lower())
        if match is None:
            raise ComposeError(f"The compose service {name} has an invalid memory value {value!r}.")
        size = float(match.group(1)) * _UNIT_BYTES[match.group(2)]
    if size <= 0:
        raise ComposeError(f"The compose service {name} has an invalid memory value {value!r}.")
    return math.ceil(size / 1024**2)


def _depends_on(name: str, value: Any) -> tuple[str, ...]:
    """Read the compose ``depends_on`` of a service.

    Args:
        name: The service name, for the error.
        value: A list of names, a mapping of names to conditions, or ``None``.

    Returns:
        The names of the services this one needs.

    Raises:
        ComposeError: The value is neither a list nor a mapping, or a name
            is not a scalar.
    """
    if value is None:
        return ()
    if isinstance(value, Mapping | list):
        return tuple(_scalar(name, "depends_on", item) for item in value)
    raise ComposeError(f"The compose service {name} has a depends_on that is not a list or a mapping.")


def _duration(name: str, value: Any, default: float) -> float:
    """Read a compose duration such as ``5s`` or ``1m30s``.

    Args:
        name: The service name, for the error.
        value: The duration text, a number of seconds, or ``None``.
        default: The value when the field is not set.

    Returns:
        The duration in seconds.

    Raises:
        ComposeError: The value is not a duration, or it is longer than
            ``MAX_HEALTHCHECK_DURATION``.
    """
    if value is None:
        return default
    if isinstance(value, int | float) and not isinstance(value, bool):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip()
        parts = _DURATION_PART.findall(text)
        if _NUMBER.match(text):
            seconds = float(text)
        elif parts and "".join(number + unit for number, unit in parts) == text:
            seconds = sum(float(number) * _UNIT_SECONDS[unit] for number, unit in parts)
        else:
            raise ComposeError(f"The compose service {name} has an invalid duration {value!r}.")
    else:
        raise ComposeError(f"The compose service {name} has an invalid duration {value!r}.")
    if not math.isfinite(seconds) or not 0 <= seconds <= MAX_HEALTHCHECK_DURATION:
        raise ComposeError(
            f"The compose service {name} has a healthcheck duration {value!r} outside "
            f"0 to {MAX_HEALTHCHECK_DURATION:g} seconds."
        )
    return seconds


def _retries(name: str, value: Any, default: int) -> int:
    """Read the compose healthcheck ``retries``.

    Args:
        name: The service name, for the error.
        value: The retry count as an integer or integer text, or ``None``.
        default: The value when the field is not set.

    Returns:
        The retry count.

    Raises:
        ComposeError: The value is not an integer from 1 to
            ``MAX_HEALTHCHECK_RETRIES``.
    """
    if value is None:
        return default
    if isinstance(value, str) and _INTEGER.match(value.strip()):
        retries = int(value)
    elif isinstance(value, int) and not isinstance(value, bool):
        retries = value
    else:
        retries = 0
    if not 1 <= retries <= MAX_HEALTHCHECK_RETRIES:
        raise ComposeError(
            f"The compose service {name} has healthcheck retries {value!r}; "
            f"use an integer from 1 to {MAX_HEALTHCHECK_RETRIES}."
        )
    return retries


def _healthcheck(name: str, value: Any) -> HealthcheckConfig | None:
    """Read the compose ``healthcheck`` of a service.

    Args:
        name: The service name, for the error.
        value: The healthcheck mapping, or ``None``.

    Returns:
        The poll with compose defaults, or ``None`` when there is no check.

    Raises:
        ComposeError: The healthcheck is not a mapping, its test is not a
            string or a known list form, or a timing value is invalid.
    """
    value = _mapping(name, "healthcheck", value)
    if value.get("disable") is True or value.get("disable") in _TRUE or value.get("test") is None:
        return None
    test = value["test"]
    if isinstance(test, list):
        test = [_scalar(name, "healthcheck test", item) for item in test]
    if isinstance(test, str):
        command = test
    elif isinstance(test, list) and test and test[0] == "NONE":
        return None
    elif isinstance(test, list) and len(test) == 2 and test[0] == "CMD-SHELL":
        command = test[1]
    elif isinstance(test, list) and len(test) > 1 and test[0] == "CMD":
        command = shlex.join(test[1:])
    else:
        raise ComposeError(f"The compose service {name} has an invalid healthcheck test.")
    defaults = HealthcheckConfig(command="")
    return HealthcheckConfig(
        command=command,
        interval_sec=_duration(name, value.get("interval"), 30.0),
        timeout_sec=_duration(name, value.get("timeout"), defaults.timeout_sec),
        start_period_sec=_duration(name, value.get("start_period"), defaults.start_period_sec),
        start_interval_sec=_duration(name, value.get("start_interval"), defaults.start_interval_sec),
        retries=_retries(name, value.get("retries"), defaults.retries),
    )


def _start_order(services: list[ComposeService]) -> tuple[ComposeService, ...]:
    """Sort services so each starts after the services it depends on.

    Args:
        services: The services in file order.

    Returns:
        The services in start order.

    Raises:
        ComposeError: A dependency is unknown or the dependencies form a cycle.
    """
    names = {service.name for service in services}
    for service in services:
        unknown = sorted(set(service.depends_on) - names)
        if unknown:
            raise ComposeError(f"The compose service {service.name} depends on unknown services: {', '.join(unknown)}.")
    ordered: list[ComposeService] = []
    placed: set[str] = set()
    pending = list(services)
    while pending:
        ready = next((service for service in pending if set(service.depends_on) <= placed), None)
        if ready is None:
            cycle = ", ".join(service.name for service in pending)
            raise ComposeError(f"The compose services have a depends_on cycle: {cycle}.")
        ordered.append(ready)
        placed.add(ready.name)
        pending.remove(ready)
    return tuple(ordered)


def box_resources(
    request: ResourceRequest, services: Sequence[ComposeService], caps: EnvironmentCapabilities
) -> ResourceRequest:
    """Grow the box size by the size of each service.

    Args:
        request: The size that the main workload asks for.
        services: The compose services that share the box.
        caps: The capabilities of the target provider.

    Returns:
        The request unchanged when there are no services, else a request
        with room for the workload and every service.

    Raises:
        ServicesTooLargeError: The total does not fit in the largest box.
    """
    if not services:
        return request
    per_vcpu = caps.memory_mb_per_vcpu
    base_memory = request.memory_mb or (request.cpus * per_vcpu if per_vcpu else 0)
    need_cpus = request.cpus + sum(service.cpus for service in services)
    need_memory = base_memory + sum(service.memory_mb for service in services)
    # Round each total before the sum of floats can add a whole spare vCPU.
    vcpus = math.ceil(round(need_cpus, 6))
    if per_vcpu:
        vcpus = max(vcpus, math.ceil(need_memory / per_vcpu))
    if caps.even_vcpus and vcpus > 1 and vcpus % 2:
        vcpus += 1
    if caps.max_vcpus is not None and vcpus > caps.max_vcpus:
        largest = f"{caps.max_vcpus} vCPUs" + (f" and {caps.max_vcpus * per_vcpu} MB" if per_vcpu else "")
        raise ServicesTooLargeError(
            f"The workload and its compose services need {need_cpus:g} vCPUs and {need_memory} MB, "
            f"but the largest {caps.name} box has {largest}. Remove a service, or lower its "
            "'cpus' or 'mem_limit' in the compose file."
        )
    mode = ResourceMode.REQUEST if request.mode is ResourceMode.IGNORE else request.mode
    return dataclasses.replace(request, cpus=vcpus, memory_mb=vcpus * per_vcpu if per_vcpu else need_memory, mode=mode)


def launch_command(service: ComposeService) -> str:
    """Build the shell text that starts one service in the background.

    Args:
        service: The service.

    Returns:
        The shell text. It writes the process id to ``service.pid_path``.
    """
    env = "".join(f"{shlex.quote(f'{key}={value}')} " for key, value in service.environment)
    # setsid makes the service a session and group leader, so the stop also
    # ends its children, and the exec time limit does not kill it.
    run = f"setsid nohup bash --noprofile --norc -c {shlex.quote(service.command)}"
    return f"env {env}{run} > {service.log_path} 2>&1 < /dev/null & echo $! > {service.pid_path}"


def healthcheck_command(service: ComposeService, command: str) -> str:
    """Build the shell text that runs a check in the directory and env of a service.

    Args:
        service: The service.
        command: The healthcheck shell text.

    Returns:
        The shell text. The env values go in the command line, not the
        ``exec`` env, so a box with services can still become a snapshot.
    """
    if not service.environment and not service.working_dir:
        return command
    cd = f"cd {shlex.quote(service.working_dir)} && " if service.working_dir else ""
    env = "".join(f"{shlex.quote(f'{key}={value}')} " for key, value in service.environment)
    return f"{cd}env {env}bash --noprofile --norc -c {shlex.quote(command)}"


def stop_command(service: ComposeService) -> str:
    """Build the shell text that stops one service and removes its files.

    Args:
        service: The service.

    Returns:
        The shell text. It sends TERM to the service group, waits up to
        five seconds, then sends KILL, so no service runs when it ends.
    """
    return (
        f'p="$(cat {service.pid_path} 2>/dev/null)"; if [ -n "$p" ]; then '
        'kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null; '
        'for _ in $(seq 50); do kill -0 "$p" 2>/dev/null || break; sleep 0.1; done; '
        'kill -KILL -- "-$p" 2>/dev/null; fi; '
        f"rm -f {service.pid_path} {service.log_path}; true"
    )


async def start_services(
    env: SandboxEnvironment, services: Sequence[ComposeService], started: list[ComposeService]
) -> None:
    """Start each service in order and wait for its healthcheck.

    The environment values go in the command line, not the ``exec`` env, so a
    box with services can still become a snapshot.

    Args:
        env: The running box.
        services: The services in start order.
        started: Receives each service as soon as its process may run, so a
            stop after a failure ends it too.

    Raises:
        SandboxInfraError: A service did not start.
        HealthcheckError: A service did not become healthy, or the services
            did not all become healthy in ``SERVICES_START_TIMEOUT``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SERVICES_START_TIMEOUT
    for service in services:
        started.append(service)
        result = await env.exec(launch_command(service), cwd=service.working_dir, timeout_sec=LAUNCH_TIMEOUT)
        if not result.ok:
            raise SandboxInfraError(f"The compose service {service.name} did not start: exit code {result.exit_code}.")
        if service.healthcheck is not None:
            check = dataclasses.replace(
                service.healthcheck, command=healthcheck_command(service, service.healthcheck.command)
            )
            try:
                # A failed probe after the box is gone counts as one failure,
                # so the deadline is what ends the poll in that case.
                async with asyncio.timeout_at(deadline):
                    await env.healthcheck(check)
            except TimeoutError as error:
                raise HealthcheckError(
                    f"The compose service {service.name} did not become healthy in "
                    f"{SERVICES_START_TIMEOUT:g} seconds. Read {service.log_path} in the box."
                ) from error
            except HealthcheckError as error:
                raise HealthcheckError(
                    f"The compose service {service.name} is not healthy. Read {service.log_path} in the box. {error}"
                ) from error


async def stop_services(env: SandboxEnvironment, started: list[ComposeService]) -> None:
    """Stop the started services in reverse order, and empty the list.

    A failed stop is logged and the next service still stops.

    Args:
        env: The running box.
        started: The started services, in start order.
    """
    while started:
        service = started.pop()
        try:
            await env.exec(stop_command(service), timeout_sec=SERVICE_STOP_TIMEOUT)
        except Exception as error:
            logger.warning("Compose service %s did not stop: %s", service.name, type(error).__name__)
