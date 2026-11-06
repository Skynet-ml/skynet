"""Tests for protected execution on the local Docker sandbox (local mode)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from core.api.routers import execution_runtimes
from core.billing.operation_pricing import UnpricedOperationError
from core.billing.protected_execution import (
    bind_protected_sandbox,
    protected_image,
    protected_vercel_unavailable_reason,
    runtime_cost_profile,
)
from core.config import Settings
from core.i18n import t
from core.service_gateway.optimization.blackbox import agent_eval
from core.service_gateway.optimization.blackbox import service as blackbox_service
from core.service_gateway.optimization.blackbox.docker_sandbox import DockerSandboxRuntime
from core.service_gateway.optimization.blackbox.sandbox import JOB_TAG

_IMAGE = "sha256:" + "c" * 64
_SETTINGS_ENV = (
    "VERCEL_TOKEN",
    "VERCEL_TEAM_ID",
    "VERCEL_PROJECT_ID",
    "APP_LOCAL_MODE",
    "SKYNET_LOCAL_MODE",
    "APP_LOCAL_SANDBOX_IMAGE",
    "SKYNET_LOCAL_SANDBOX_IMAGE",
    "APP_DOCKER_BIN",
    "SKYNET_DOCKER_BIN",
    "RAILWAY_ENVIRONMENT_NAME",
    "OPENROUTER_API_KEY",
)
_FAKE_DOCKER = """#!{python}
import json, os, sys
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "run":
    print("box" * 20)
"""


class _NoChargeBudget:
    """Budget stand-in that fails the test if a local box touches it."""

    def __getattr__(self, name: str) -> Any:
        """Refuse every budget operation.

        Args:
            name: The attribute the runtime asked for.

        Raises:
            AssertionError: Always; local boxes must never reserve or settle.
        """
        raise AssertionError(f"local sandbox touched the budget: {name}")


class _Gateway:
    """Record what ``bind_protected_sandbox`` hands the model gateway."""

    def __init__(self) -> None:
        """Start with no bound broker."""
        self.runtime = _NoChargeBudget()
        self.broker: Any = None
        self.bound: dict[str, Any] = {}
        self.parent_hosts: tuple[str, ...] | None = None

    def dispatch_guest(self, *_args: Any) -> None:
        """Accept guest model calls; none happen in these tests."""

    def dispatch_route(self, *_args: Any) -> None:
        """Accept parent box model calls; none happen in these tests."""

    def bind_sandbox(self, broker: Any, **values: Any) -> None:
        """Keep the broker and its profile.

        Args:
            broker: The parent-owned sandbox broker.
            **values: Image and lifetime.
        """
        self.broker = broker
        self.bound = values

    def fund_parent_sandbox(self, hosts: tuple[str, ...]) -> None:
        """Keep the parent box's hosts.

        Args:
            hosts: Package registries.
        """
        self.parent_hosts = hosts


def _settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> Settings:
    """Build settings from a clean environment, ignoring the developer's ``.env``.

    Args:
        monkeypatch: Pytest fixture.
        **values: Settings fields to set.

    Returns:
        A fresh ``Settings`` instance.
    """
    for name in _SETTINGS_ENV:
        monkeypatch.delenv(name, raising=False)
    if values.get("local_mode"):
        monkeypatch.setenv("API_HOST", "127.0.0.1")
    return Settings(_env_file=None, **values)


@pytest.fixture
def local_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Settings, Path]:
    """Build local-mode settings over a recording fake Docker CLI and no Vercel token.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest fixture.

    Returns:
        The settings and the fake CLI's call log.
    """
    binary = tmp_path / "docker"
    binary.write_text(_FAKE_DOCKER.format(python=sys.executable))
    binary.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    log.touch()
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    settings = _settings(monkeypatch, local_mode=True, docker_bin=str(binary), local_sandbox_image=_IMAGE)
    return settings, log


def test_local_mode_pins_the_bare_local_image_for_every_workflow(local_settings: tuple[Settings, Path]) -> None:
    """Accept a bare ``sha256:`` id in local mode, for DSPy and Anything alike."""
    settings, _ = local_settings
    assert protected_image(settings, "dspy") == _IMAGE
    assert protected_image(settings, "anything") == _IMAGE
    assert protected_vercel_unavailable_reason(settings, "dspy") is None


def test_hosted_mode_refuses_a_bare_image_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hosted deployment still requires a registry digest reference."""
    settings = _settings(monkeypatch, vercel_sandbox_image=_IMAGE, dspy_sandbox_image=_IMAGE)
    assert protected_image(settings, "dspy") is None
    assert protected_image(settings, "anything") is None


def test_local_mode_reports_a_missing_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explain the Docker problem instead of a Vercel one."""
    settings = _settings(monkeypatch, local_mode=True, docker_bin=str(tmp_path / "missing"), local_sandbox_image=_IMAGE)
    assert protected_vercel_unavailable_reason(settings, "dspy") == t("sandbox.docker_unavailable")


def test_docker_cost_profile_is_zero(local_settings: tuple[Settings, Path]) -> None:
    """A local box adds no sandbox cost to the run."""
    settings, _ = local_settings
    profile = runtime_cost_profile(settings, "dspy", "docker")
    assert profile["minimum_session_cents"] == "0"
    assert profile["maximum_session_cents"] == "0"


def test_bind_uses_docker_without_a_vercel_token_and_never_charges(local_settings: tuple[Settings, Path]) -> None:
    """Bind a Docker broker with the mailbox, and open, write and close a box with zero settled cost."""
    settings, log = local_settings
    assert settings.vercel_token is None
    gateway = _Gateway()

    identity = bind_protected_sandbox(gateway, settings, workflow="dspy", owner_id="job-9", parent_hosts=("pypi.org",))

    assert identity["image"] == _IMAGE
    assert gateway.bound == {"image": _IMAGE, "lifetime_seconds": identity["lifetime_seconds"]}
    assert gateway.parent_hosts == ("pypi.org",)
    runtime = gateway.broker._runtime
    assert isinstance(runtime, DockerSandboxRuntime)
    assert runtime.protected
    assert gateway.broker._command_runner is not None

    opened = gateway.broker.handle("open", {"request_id": "r1", "spec": {"lifetime_seconds": 60}})
    gateway.broker.handle("write", {"sandbox_id": opened["sandbox_id"], "files": {"a.txt": "x"}})
    gateway.broker.handle("close", {"sandbox_id": opened["sandbox_id"]})
    gateway.broker.close()

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    (run,) = (call for call in calls if call[0] == "run")
    assert run[run.index("--network") + 1] == "none"
    assert "skynet.job=job-9" in run
    assert f"{JOB_TAG}=job-9" in run
    assert ["rm", "-f", "box" * 20] in calls


def test_hosted_bind_refuses_a_bare_pinned_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted mode never boots a local image id, even when one is pinned on the run."""
    settings = _settings(monkeypatch)
    with pytest.raises(UnpricedOperationError):
        bind_protected_sandbox(_Gateway(), settings, workflow="dspy", owner_id="job-9", image=_IMAGE)


def test_runtime_catalog_reports_docker_in_local_mode(
    local_settings: tuple[Settings, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local mode lists the Docker runtime as available without an OpenRouter key."""
    settings, _ = local_settings
    monkeypatch.setattr(execution_runtimes, "settings", settings)

    catalog = execution_runtimes.execution_runtime_catalog()

    (runtime,) = catalog["runtimes"]
    assert runtime["id"] == catalog["default_runtime"] == "docker"
    assert runtime["available"] is True
    assert runtime["cost"]["maximum_session_cents"] == "0"


def test_engine_catalog_names_the_docker_proposer_runtime_in_local_mode(
    local_settings: tuple[Settings, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine catalog reports the Docker runtime, free, in local mode."""
    settings, _ = local_settings
    monkeypatch.setattr(blackbox_service, "settings", settings)

    (runtime,) = blackbox_service.engine_catalog("text").proposer_runtimes

    assert runtime.id == "docker"
    assert runtime.cost.maximum_session_cents == "0"


_NO_GATEWAY: dict[str, Any] = {
    "blackbox_agent_gateway_url": None,
    "blackbox_agent_gateway_api_key": None,
    "litellm_proxy_url": None,
    "litellm_proxy_api_key": None,
}


def test_engine_catalog_reports_the_docker_sandbox_available_in_local_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local mode with a working Docker runtime needs no deployment gateway, so it shows no sandbox warning."""
    binary = tmp_path / "docker"
    binary.write_text(_FAKE_DOCKER.format(python=sys.executable))
    binary.chmod(0o755)
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "calls.jsonl"))
    settings = _settings(
        monkeypatch, local_mode=True, docker_bin=str(binary), local_sandbox_image=_IMAGE, **_NO_GATEWAY
    )
    monkeypatch.setattr(blackbox_service, "settings", settings)

    catalog = blackbox_service.engine_catalog("agent")

    assert catalog.sandbox_available is True
    assert catalog.sandbox_reason is None


def test_engine_catalog_still_needs_a_gateway_on_hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hosted deployment with sandboxes but no model gateway keeps the gateway warning."""
    settings = _settings(monkeypatch, **_NO_GATEWAY)
    monkeypatch.setattr(blackbox_service, "settings", settings)
    monkeypatch.setattr(agent_eval, "sandbox_unavailable_reason", lambda settings: None)

    catalog = blackbox_service.engine_catalog("agent")

    assert catalog.sandbox_available is False
    assert catalog.sandbox_reason == agent_eval._GATEWAY_MISSING
