"""Tests for planning each role's box: hosts, MCP servers, service secrets and stage boxes."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from core.service_gateway.optimization.blackbox import repo_services
from core.service_gateway.optimization.blackbox.protocol import BudgetExhaustedError
from core.service_gateway.optimization.blackbox.repo_constraints import ServiceSpec, parse_constraints
from core.service_gateway.optimization.blackbox.sandbox_contract import SandboxSpec

_FILE = b"""
version = 1

[proposer]
tools = ["bash", "maps", "maps", "unknown-mcp"]
network = ["pypi.org"]
services = ["maps"]

[target.planner]
entry = "agents/planner.py:plan"
tools = ["web_search"]
network = []
services = ["maps"]
vcpus = 4

[target.router]
entry = ["node", "router.js"]

[services.maps]
secrets = ["GOOGLE_MAPS_API_KEY"]
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

    def __init__(self, refuse_at: int | None = None) -> None:
        """Set which open the budget refuses.

        Args:
            refuse_at: Zero-based index of the refused open, or ``None``.
        """
        self.refuse_at = refuse_at
        self.sessions: list[_Session] = []

    def open(self, spec: SandboxSpec) -> _Session:
        """Open a fake box, or refuse it like a spent budget.

        Args:
            spec: The box's spec.

        Returns:
            The fake box.

        Raises:
            BudgetExhaustedError: At the refused open.
        """
        if len(self.sessions) == self.refuse_at:
            raise BudgetExhaustedError("budget reached")
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
        name: repo_services.stage_spec(name, targets[name], candidate_id="c1", lifetime_seconds=60, tags={"job": "j"})
        for name in names
    }


def test_each_role_gets_only_its_listed_hosts_servers_and_services() -> None:
    """The record names every role's plan; the proposer keeps its list, and every target is offline."""
    plans = repo_services.plan_roles(parse_constraints(_FILE))

    assert plans == {
        "proposer": {"allowed_hosts": ["pypi.org"], "mcp_servers": ["maps"], "services": ["maps"]},
        "targets": {
            "planner": {"allowed_hosts": [], "mcp_servers": ["web_search"], "services": ["maps"]},
            "router": {"allowed_hosts": [], "mcp_servers": [], "services": []},
        },
    }


def test_an_unlisted_mcp_server_is_absent() -> None:
    """Only catalog servers that the role lists reach its harness; built-ins and unknown names do not."""
    assert repo_services.mcp_servers(["bash", "web_search"]) == ("web_search",)
    assert "maps" not in repo_services.mcp_servers(["bash", "web_search"])
    assert repo_services.mcp_servers(["unknown-mcp"]) == ()


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


def test_each_candidate_gets_its_own_stage_box() -> None:
    """A stage box has no fixed name, and its hold key names the candidate and the stage (E29)."""
    stage = parse_constraints(_FILE).targets["planner"]

    first = repo_services.stage_spec("planner", stage, candidate_id="c1", lifetime_seconds=60, tags={})
    second = repo_services.stage_spec("planner", stage, candidate_id="c2", lifetime_seconds=60, tags={})

    assert first.name is None
    assert second.name is None
    assert first.operation_key == "stage:c1:planner"
    assert first.operation_key != second.operation_key


def test_a_target_network_list_gives_its_box_no_host() -> None:
    """A target box is offline except for the relay, so the plan and the spec both hold no host."""
    raw = b'version = 1\n[target.web]\nentry = "a.py:f"\nnetwork = ["api.example.com"]\n'
    constraints = parse_constraints(raw)

    plans = repo_services.plan_roles(constraints)
    spec = repo_services.stage_spec("web", constraints.targets["web"], candidate_id="c1", lifetime_seconds=60, tags={})

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


def test_a_service_that_names_no_connector_fails() -> None:
    """A service is a catalog connector, so an unknown name stops before any key is handed out."""
    with pytest.raises(repo_services.ServiceSecretError, match="Service cache is not a connector"):
        repo_services.service_environment("cache", ServiceSpec(), {})


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

    assert repo_services.probe_connector("maps", send)
    assert sent == [("https://mapstools.googleapis.com/mcp", {"X-Key": _SECRET})]


def test_a_connector_without_a_key_or_an_answer_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown name, a missing parent key, a no, or a raising sender all fail the health check."""

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

    assert not repo_services.probe_connector("cache", lambda url, headers: True)
    monkeypatch.setattr(repo_services, "connector_endpoint", lambda connector: (connector.url, {}))
    assert not repo_services.probe_connector("maps", lambda url, headers: False)
    assert not repo_services.probe_connector("maps", unreachable)
    monkeypatch.setattr(repo_services, "connector_endpoint", no_key)
    assert not repo_services.probe_connector("maps", lambda url, headers: True)


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
    assert "maps" in proposer.mcp_servers
    assert "web_search" not in proposer.mcp_servers
    spec = repo_services.stage_spec("planner", planner, candidate_id="c1", lifetime_seconds=60, tags={})
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

    with pytest.raises(BudgetExhaustedError):
        boxes = repo_services.open_stage_boxes(runtime, _specs(("planner", "router")))

    assert boxes is None
    assert len(runtime.sessions) == 1
    assert runtime.sessions[0].closed
