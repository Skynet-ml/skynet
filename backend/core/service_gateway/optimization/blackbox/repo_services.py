"""Plan each role's box from ``optimize.toml``: hosts, MCP servers, service secrets and stage boxes.

``[proposer]`` and each ``[target.<name>]`` get their own box (E1, E2). This
module turns a role's parsed environment into what its box gets, and opens a
candidate's stage boxes all or none (E29-E32). It holds no secret value in
anything it returns for a box: only a service's own environment carries the
values it names (D28).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

from ....billing.env_connectors import CONNECTORS, connector_endpoint
from .docker_egress_proxy import target_allowed
from .repo_constraints import RepoConstraints, RoleEnvironment, ServiceSpec, TargetStage
from .sandbox_contract import SandboxRuntime, SandboxSession, SandboxSpec


@dataclass(frozen=True)
class RolePlan:
    """What one role's box gets.

    ``allowed_hosts`` is ``None`` when the file sets no list, so the role keeps
    its default network; an empty tuple means offline.
    """

    allowed_hosts: tuple[str, ...] | None
    mcp_servers: tuple[str, ...]
    services: tuple[str, ...]

    def record(self) -> dict[str, object]:
        """Describe the plan as JSON for the run's environment record.

        Returns:
            The hosts, MCP servers and services, by name only.
        """
        return {
            "allowed_hosts": None if self.allowed_hosts is None else list(self.allowed_hosts),
            "mcp_servers": list(self.mcp_servers),
            "services": list(self.services),
        }


class ServiceSecretError(ValueError):
    """A service names a secret that has no value; the message names the service and the secret."""


def plan_role(role: RoleEnvironment) -> RolePlan:
    """Work out one role's box from its parsed environment.

    Args:
        role: The ``[proposer]`` or ``[target.<name>]`` environment.

    Returns:
        The role's hosts, MCP servers and services.
    """
    return RolePlan(allowed_hosts=role.network, mcp_servers=mcp_servers(role.tools), services=role.services)


def plan_roles(constraints: RepoConstraints) -> dict[str, object]:
    """Describe every role's plan as JSON for the run's environment record.

    Args:
        constraints: The repository's parsed ``optimize.toml``.

    Returns:
        ``{"proposer": plan, "targets": {name: plan}}``.
    """
    return {
        "proposer": plan_role(constraints.proposer).record(),
        "targets": {name: plan_role(stage).record() for name, stage in constraints.targets.items()},
    }


def mcp_servers(tools: Collection[str]) -> tuple[str, ...]:
    """Pick the tool entries that name a catalog MCP server, in file order.

    Any other entry is a harness built-in, so a server that is not listed never
    reaches the role's harness.

    Args:
        tools: The role's ``tools`` list.

    Returns:
        The distinct catalog server names.
    """
    return tuple(name for name in dict.fromkeys(tools) if name in CONNECTORS)


def host_allowed(host: str, allowed: Collection[str]) -> bool:
    """Report whether a box with this allowlist may reach ``host``, as the egress proxy decides.

    Args:
        host: ``name`` or ``name:port`` the box asks for; no port means 443.
        allowed: The role's allowlist.

    Returns:
        Whether the host is on the list and the port is 443.
    """
    name, _, port = host.rpartition(":") if ":" in host else (host, "", "443")
    if not port.isdigit():
        return False
    return target_allowed(
        (name.rstrip(".").lower(), int(port)), frozenset(entry.rstrip(".").lower() for entry in allowed)
    )


def unenforceable_hosts(allowed: Collection[str]) -> list[str]:
    """List the allowlist entries the egress cannot hold the box to.

    ``optimize.toml`` accepts ``*.example.com`` and ``host:port``, but the
    egress proxy and the edge policy allow only exact hosts on 443, so such an
    entry must stop the run instead of silently allowing less or more.

    Args:
        allowed: The role's allowlist.

    Returns:
        The wildcard entries and the entries with a port, in order.
    """
    return [entry for entry in allowed if entry.startswith("*.") or ":" in entry]


def service_environment(name: str, spec: ServiceSpec, secrets: Mapping[str, str]) -> dict[str, str]:
    """Build one service's environment from only the secrets it names.

    A service is a connector in the catalog: ``[services.maps]`` is the
    ``maps`` connector, and no sidecar process, command or image runs for it.

    Args:
        name: The ``[services.<name>]`` table name, a connector name.
        spec: Its declaration.
        secrets: Every secret this run has, by name.

    Returns:
        The named secrets, and nothing else.

    Raises:
        ServiceSecretError: When the name is not a connector, or a named
            secret has no value.
    """
    if name not in CONNECTORS:
        raise ServiceSecretError(f"Service {name} is not a connector; known: {', '.join(CONNECTORS)}.")
    missing = [secret for secret in spec.secrets if not secrets.get(secret)]
    if missing:
        raise ServiceSecretError(f"Service {name} needs the secret {missing[0]}, which has no value.")
    return {secret: secrets[secret] for secret in spec.secrets}


def probe_connector(name: str, send: Callable[[str, Mapping[str, str]], bool]) -> bool:
    """Check that one service's connector answers, with the key the parent holds.

    The parent sends the probe, so the key never enters a box.

    Args:
        name: The service's connector name.
        send: Sends one request to the URL with the headers, and returns
            whether the connector answered as healthy.

    Returns:
        ``False`` when the name is not a connector, Skynet holds no key for
        it, or ``send`` says no or raises.
    """
    connector = CONNECTORS.get(name)
    if connector is None:
        return False
    try:
        url, headers = connector_endpoint(connector)
        return bool(send(url, headers))
    except Exception:
        return False


def unhealthy_services(services: Collection[str], probe: Callable[[str], bool]) -> list[str]:
    """List the services whose health probe fails, in order.

    A probe that raises counts as unhealthy, so a broken service never passes
    the gate.

    Args:
        services: The service names to check.
        probe: Returns whether one named service is healthy, for example
            :func:`probe_connector` with a sender.

    Returns:
        The names of the unhealthy services.
    """
    failed = []
    for name in services:
        try:
            healthy = probe(name)
        except Exception:
            healthy = False
        if not healthy:
            failed.append(name)
    return failed


def stage_spec(name: str, stage: TargetStage, *, lifetime_seconds: float, tags: Mapping[str, str]) -> SandboxSpec:
    """Build the spec of one stage box for one candidate evaluation.

    A target is offline except for the relay, which the parent's mailbox
    serves, so the box gets no host and no secret (E27).

    Args:
        name: The ``[target.<name>]`` table name.
        stage: Its parsed settings.
        lifetime_seconds: The candidate evaluation's limit, which is also the
            box's hold (E31).
        tags: The job's labels, so the sweep finds the box.

    Returns:
        The stage box's spec.
    """
    return SandboxSpec(
        lifetime_seconds=lifetime_seconds,
        name=f"app-stage-{name}",
        tags=dict(tags),
        vcpus=stage.vcpus,
        network_disabled=True,
    )


def open_stage_boxes(runtime: SandboxRuntime, specs: Mapping[str, SandboxSpec]) -> dict[str, SandboxSession]:
    """Open every stage box of one candidate before its first sample, or none.

    When one box cannot open, the boxes that opened close, so the candidate
    has no partial score and leaves no box behind (E32).

    Args:
        runtime: The run's sandbox runtime; each open reserves its own hold.
        specs: Each stage's box spec, by stage name.

    Returns:
        Each stage's open box, by stage name.

    Raises:
        BudgetExhaustedError: When the budget refuses a box; the run stops as
            ``budget_reached`` with the best candidate so far.
    """
    opened: dict[str, SandboxSession] = {}
    try:
        for name, spec in specs.items():
            opened[name] = runtime.open(spec)
    except BaseException:
        for session in opened.values():
            with contextlib.suppress(Exception):
                session.close()
        raise
    return opened
