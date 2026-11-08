"""Tests for planning each role's box: hosts, MCP servers, service secrets and stage boxes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace

import pytest

from core.billing.signals import BudgetReached
from core.service_gateway.optimization.blackbox import repo_services
from core.service_gateway.optimization.blackbox.repo_constraints import (
    McpServer,
    RoleEnvironment,
    ServiceSpec,
    parse_constraints,
)
from core.service_gateway.optimization.blackbox.sandbox_contract import SandboxSpec

_FILE = b"""
version = 1

[proposer]
network = ["pypi.org"]
services = ["maps"]

[target.planner]
entry = "agents/planner.py:plan"
network = []
services = ["maps"]
vcpus = 4

[target.router]
entry = ["node", "router.js"]

[services.maps]
url = "https://mapstools.googleapis.com/mcp"
headers = { Authorization = "Bearer ${GOOGLE_MAPS_API_KEY}" }
"""

_SECRET = "maps-secret-value"


class _Session:
    """A fake open box that records whether it closed."""

    def __init__(self, spec: SandboxSpec) -> None:
        """Keep the spec the box opened with.

        Args:
            spec: The box's spec.
        """
        self.spec = spec
        self.closed = False

    def close(self) -> None:
        """Mark the box closed."""
        self.closed = True


class _Runtime:
    """A fake runtime whose budget refuses the box at ``refuse_at``."""

    injects_headers = False

    def __init__(self, refuse_at: int | None = None, error: BaseException | None = None) -> None:
        """Set which open fails, and how.

        Args:
            refuse_at: Zero-based index of the failed open, or ``None``.
            error: What the failed open raises; a budget stop by default.
        """
        self.refuse_at = refuse_at
        self.error = error or BudgetReached("budget reached")
        self.sessions: list[_Session] = []

    def open(self, spec: SandboxSpec) -> _Session:
        """Open a fake box, or refuse it like a spent budget.

        Args:
            spec: The box's spec.

        Returns:
            The fake box.

        Raises:
            BaseException: The set error, at the failed open.
        """
        if len(self.sessions) == self.refuse_at:
            raise self.error
        session = _Session(spec)
        self.sessions.append(session)
        return session


def _specs(names: tuple[str, ...]) -> Mapping[str, SandboxSpec]:
    """Build each stage's box spec from the fixture file.

    Args:
        names: The stage names, in open order.

    Returns:
        Each stage's spec, by name.
    """
    targets = parse_constraints(_FILE).targets
    return {
        name: repo_services.stage_spec(
            name, targets[name], evaluation_id="c1-e1", lifetime_seconds=60, tags={"job": "j"}
        )
        for name in names
    }


def test_each_role_gets_only_its_listed_hosts_servers_and_services() -> None:
    """The record names every role's plan; the proposer keeps its list, and every target is offline."""
    plans = repo_services.plan_roles(parse_constraints(_FILE))

    assert plans == {
        "proposer": {"allowed_hosts": ["pypi.org"], "mcp_servers": [], "services": ["maps"]},
        "targets": {
            "planner": {"allowed_hosts": [], "mcp_servers": [], "services": ["maps"]},
            "router": {"allowed_hosts": [], "mcp_servers": [], "services": []},
        },
    }


def test_a_role_gets_only_the_servers_of_its_own_mcp_file() -> None:
    """A role's plan lists only the servers of its own MCP file (E49, E50)."""
    constraints = parse_constraints(_FILE)
    run = replace(
        constraints.run_role,
        mcp_servers=(McpServer(name="grader", url="https://mcp.example.com/mcp", headers={}),),
    )

    assert repo_services.plan_role(constraints.proposer).mcp_servers == ()
    assert repo_services.plan_role(run).mcp_servers == ("grader",)


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("pypi.org", True),
        ("PyPI.org.", True),
        ("pypi.org:443", True),
        ("evil.example.com", False),
        ("pypi.org:80", False),
        ("pypi.org:http", False),
        ("sub.pypi.org", False),
    ],
)
def test_an_unlisted_host_is_refused(host: str, allowed: bool) -> None:
    """The plan decides like the egress proxy: exact listed hosts on 443 only."""
    assert repo_services.host_allowed(host, ["pypi.org"]) is allowed


def test_an_empty_allowlist_refuses_every_host() -> None:
    """``network = []`` means offline."""
    assert not repo_services.host_allowed("pypi.org", [])


def test_wildcards_and_ports_are_flagged_as_unenforceable() -> None:
    """The egress holds only exact hosts on 443, so other forms must stop the run."""
    assert repo_services.unenforceable_hosts(["pypi.org", "*.example.com", "db.local:5432"]) == [
        "*.example.com",
        "db.local:5432",
    ]


def test_no_secret_reaches_a_stage_box() -> None:
    """A stage box is offline and gets no environment, so no service key is inside it."""
    for spec in _specs(("planner", "router")).values():
        assert spec.env == {}
        assert spec.inject_headers == {}
        assert spec.allowed_hosts == ()
        assert spec.network_disabled
        assert _SECRET not in repr(spec)
    assert _specs(("planner",))["planner"].vcpus == 4


def test_each_evaluation_gets_its_own_stage_box() -> None:
    """A stage box has no fixed name, and its hold key names the evaluation and the stage (E29)."""
    stage = parse_constraints(_FILE).targets["planner"]

    first = repo_services.stage_spec("planner", stage, evaluation_id="c1-e1", lifetime_seconds=60, tags={})
    second = repo_services.stage_spec("planner", stage, evaluation_id="c2-e1", lifetime_seconds=60, tags={})

    assert first.name is None
    assert second.name is None
    again = repo_services.stage_spec("planner", stage, evaluation_id="c1-e2", lifetime_seconds=60, tags={})

    assert first.operation_key == "stage:c1-e1:planner"
    assert first.operation_key != second.operation_key
    assert again.operation_key != first.operation_key


def test_a_target_network_list_gives_its_box_no_host() -> None:
    """A target box is offline except for the relay, so the plan and the spec both hold no host."""
    raw = b'version = 1\n[target.web]\nentry = "a.py:f"\nnetwork = ["api.example.com"]\n'
    constraints = parse_constraints(raw)

    plans = repo_services.plan_roles(constraints)
    spec = repo_services.stage_spec(
        "web", constraints.targets["web"], evaluation_id="c1-e1", lifetime_seconds=60, tags={}
    )

    assert plans["targets"] == {"web": {"allowed_hosts": [], "mcp_servers": [], "services": []}}
    assert spec.allowed_hosts == ()
    assert spec.network_disabled


def test_a_service_gets_only_the_secrets_it_names() -> None:
    """The edge-held service environment holds its own keys and no other run secret."""
    secrets = {"GOOGLE_MAPS_API_KEY": _SECRET, "API_KEY": "other-value"}

    environment = repo_services.service_environment("maps", ServiceSpec(secrets=("GOOGLE_MAPS_API_KEY",)), secrets)

    assert list(environment) == ["GOOGLE_MAPS_API_KEY"]
    assert "other-value" not in environment.values()


@pytest.mark.parametrize("secrets", [{}, {"GOOGLE_MAPS_API_KEY": ""}])
def test_a_service_without_its_secret_fails_without_printing_values(secrets: dict[str, str]) -> None:
    """A missing or empty key stops the service, and the message names it but holds no value."""
    with pytest.raises(repo_services.ServiceSecretError, match="maps needs the secret GOOGLE_MAPS_API_KEY") as raised:
        repo_services.service_environment("maps", ServiceSpec(secrets=("GOOGLE_MAPS_API_KEY",)), secrets)
    assert _SECRET not in str(raised.value)


def test_the_connector_probe_uses_the_parent_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe reaches the connector with the parent-held headers and reports what the sender says."""
    sent: list[tuple[str, Mapping[str, str]]] = []
    monkeypatch.setattr(repo_services, "connector_endpoint", lambda connector: (connector.url, {"X-Key": _SECRET}))

    def send(url: str, headers: Mapping[str, str]) -> bool:
        """Record the probe request.

        Args:
            url: The connector URL.
            headers: The credential headers.

        Returns:
            ``True``, a healthy connector.
        """
        sent.append((url, headers))
        return True

    assert repo_services.probe_connector("mapstools.googleapis.com", send)
    assert sent == [("https://mapstools.googleapis.com/mcp", {"X-Key": _SECRET})]


def test_a_connector_without_a_key_or_an_answer_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A name that is not a catalog host, a missing parent key, a no, or a raising sender all fail the health check."""

    def no_key(connector: object) -> tuple[str, dict[str, str]]:
        """Fail like a connector with no key in config.

        Args:
            connector: The catalog entry.

        Raises:
            ValueError: Always.
        """
        raise ValueError("no key")

    def unreachable(url: str, headers: Mapping[str, str]) -> bool:
        """Fail like a connector that cannot be reached.

        Args:
            url: The connector URL.
            headers: The credential headers.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("no route")

    assert not repo_services.probe_connector("maps", lambda url, headers: True)
    monkeypatch.setattr(repo_services, "connector_endpoint", lambda connector: (connector.url, {}))
    assert not repo_services.probe_connector("mapstools.googleapis.com", lambda url, headers: False)
    assert not repo_services.probe_connector("mapstools.googleapis.com", unreachable)
    monkeypatch.setattr(repo_services, "connector_endpoint", no_key)
    assert not repo_services.probe_connector("mapstools.googleapis.com", lambda url, headers: True)


def test_an_unhealthy_service_fails_the_check() -> None:
    """A probe that says no, or that raises, marks the service unhealthy."""

    def probe(name: str) -> bool:
        """Answer the fake health probe.

        Args:
            name: The service name.

        Returns:
            Whether the service is healthy.

        Raises:
            ConnectionError: For the service that cannot be reached.
        """
        if name == "broken":
            raise ConnectionError("no route")
        return name == "maps"

    assert repo_services.unhealthy_services(["maps", "down", "broken"], probe) == ["down", "broken"]
    assert repo_services.unhealthy_services(["maps"], probe) == []


def test_allowed_calls_work_forbidden_calls_fail_and_no_key_is_exposed() -> None:
    """The SKY-82 pattern for one role: its listed host and server pass, others fail, the box holds no key."""
    constraints = parse_constraints(_FILE)
    proposer = repo_services.plan_role(constraints.proposer)
    planner = constraints.targets["planner"]

    assert proposer.allowed_hosts is not None
    assert repo_services.host_allowed("pypi.org", proposer.allowed_hosts)
    assert not repo_services.host_allowed("maps.googleapis.com", proposer.allowed_hosts)
    assert proposer.mcp_servers == ()
    spec = repo_services.stage_spec("planner", planner, evaluation_id="c1-e1", lifetime_seconds=60, tags={})
    service = repo_services.service_environment("maps", constraints.services["maps"], {"GOOGLE_MAPS_API_KEY": _SECRET})
    assert service == {"GOOGLE_MAPS_API_KEY": _SECRET}
    assert _SECRET not in repr(spec)
    assert _SECRET not in repr(proposer.record())


def test_every_stage_box_opens_before_the_first_sample() -> None:
    """When the budget allows them all, each stage gets its own open box."""
    runtime = _Runtime()

    boxes = repo_services.open_stage_boxes(runtime, _specs(("planner", "router")))

    assert list(boxes) == ["planner", "router"]
    assert not any(session.closed for session in runtime.sessions)


def test_a_refused_second_stage_box_leaves_no_partial_score_and_no_open_box() -> None:
    """The first box closes, nothing is returned to score, and the budget stop reaches the run."""
    runtime = _Runtime(refuse_at=1)
    boxes: dict[str, object] | None = None

    with pytest.raises(BudgetReached):
        boxes = repo_services.open_stage_boxes(runtime, _specs(("planner", "router")))

    assert boxes is None
    assert len(runtime.sessions) == 1
    assert runtime.sessions[0].closed


def test_any_failed_open_closes_the_boxes_that_opened() -> None:
    """An ordinary error from ``runtime.open`` also closes the open boxes and reaches the caller."""
    runtime = _Runtime(refuse_at=1, error=ConnectionError("no capacity"))

    with pytest.raises(ConnectionError):
        repo_services.open_stage_boxes(runtime, _specs(("planner", "router")))

    assert runtime.sessions[0].closed


_JUDGE_FILE = b"""
version = 1

[run]
entry = "run.py:main"
services = ["judge"]

[metric]
files = ["score.py"]

[target.planner]
entry = "agents/planner.py:plan"
services = ["maps"]

[services.judge]
url = "https://judge.example.com/v1"
headers = { Authorization = "Bearer ${JUDGE_KEY}" }

[services.maps]
url = "https://mapstools.googleapis.com/mcp"
headers = { Authorization = "Bearer ${GOOGLE_MAPS_API_KEY}" }
"""


def _with_servers(role: RoleEnvironment, *servers: McpServer) -> RoleEnvironment:
    """Give a role an MCP file that holds ``servers``.

    Args:
        role: The role to copy.
        servers: The servers its file holds.

    Returns:
        The role with ``mcp`` set and its servers filled.
    """
    return replace(role, mcp=".mcp.json", mcp_servers=servers)


def test_the_box_mcp_file_points_at_the_relay_and_holds_no_secret() -> None:
    """Each entry is an http url on the relay; no header, secret or placeholder enters the box (E49, E53)."""
    constraints = parse_constraints(_FILE)
    server = McpServer(
        name="maps", url="https://mapstools.googleapis.com/mcp", headers=(("X-Key", "${GOOGLE_MAPS_API_KEY}"),)
    )
    proposer = _with_servers(constraints.proposer, server)

    config = repo_services.box_mcp_config(proposer, "http://relay.internal:9000/")

    assert config == {"mcpServers": {"maps": {"type": "http", "url": "http://relay.internal:9000/mcp/maps"}}}
    text = json.dumps(config)
    assert "GOOGLE_MAPS_API_KEY" not in text
    assert "googleapis" not in text
    assert repo_services.box_mcp_config(constraints.targets["router"], "http://relay.internal:9000") == {
        "mcpServers": {}
    }


def test_a_metric_with_no_services_key_cannot_reach_the_judge() -> None:
    """The ``[metric]`` role does not inherit ``[run]`` services, and the error names the fix (E55)."""
    constraints = parse_constraints(_JUDGE_FILE)

    with pytest.raises(repo_services.RoleRelayError, match=r'\[metric\] services = \["judge"\]'):
        repo_services.service_endpoint("[metric]", constraints.metric_role, constraints.services, "judge")

    url, headers = repo_services.service_endpoint("[run]", constraints.run_role, constraints.services, "judge")
    assert url == "https://judge.example.com/v1"
    assert headers == {"Authorization": "Bearer ${JUDGE_KEY}"}


def test_a_target_reaches_only_the_services_it_lists() -> None:
    """``[target.planner]`` reaches ``maps``, not ``judge``; an unknown service names the table to add."""
    constraints = parse_constraints(_JUDGE_FILE)
    planner = constraints.targets["planner"]

    with pytest.raises(repo_services.RoleRelayError, match=r'\[target\.planner\] services = \["maps", "judge"\]'):
        repo_services.service_endpoint("[target.planner]", planner, constraints.services, "judge")
    with pytest.raises(repo_services.RoleRelayError, match=r"Add a \[services\.search\] table"):
        repo_services.service_endpoint("[target.planner]", planner, constraints.services, "search")
    assert repo_services.service_endpoint("[target.planner]", planner, constraints.services, "maps")[0] == (
        "https://mapstools.googleapis.com/mcp"
    )


def test_each_role_reaches_only_the_servers_in_its_own_mcp_file() -> None:
    """A proposer server is not reachable from a target, and each error names the file to change (E49)."""
    constraints = parse_constraints(_FILE)
    server = McpServer(name="docs", url="https://docs.example.com/mcp", headers=(("Authorization", "Bearer ${DOCS}"),))
    proposer = _with_servers(constraints.proposer, server)
    planner = _with_servers(constraints.targets["planner"])

    assert repo_services.mcp_endpoint("[proposer]", proposer, "docs") == (
        "https://docs.example.com/mcp",
        {"Authorization": "Bearer ${DOCS}"},
    )
    with pytest.raises(repo_services.RoleRelayError, match=r"not in \.mcp\.json, the \[target\.planner\] MCP file"):
        repo_services.mcp_endpoint("[target.planner]", planner, "docs")
    with pytest.raises(repo_services.RoleRelayError, match=r'set \[target\.router\] mcp = "\.mcp\.json"'):
        repo_services.mcp_endpoint("[target.router]", constraints.targets["router"], "docs")
