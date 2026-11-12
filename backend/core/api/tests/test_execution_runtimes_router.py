"""Validate execution runtime pricing and checkpoint-hosting capability metadata."""

from __future__ import annotations

from pydantic import SecretStr

from core.api.routers import execution_runtimes


def test_runtime_catalog_separates_runtime_capability_from_run_eligibility(monkeypatch) -> None:
    """Report transport support without claiming that a particular run can resume."""
    monkeypatch.setattr(execution_runtimes, "protected_vercel_unavailable_reason", lambda _settings: None)
    monkeypatch.setattr(execution_runtimes.settings, "openrouter_api_key", SecretStr("configured"))
    monkeypatch.setattr(
        execution_runtimes,
        "runtime_cost_profile",
        lambda _settings, _runtime: {
            "billing_basis": "at_cost",
            "minimum_session_cents": "0.1",
            "maximum_session_cents": "10",
            "maximum_lifetime_seconds": 2700,
            "vcpus": 2,
        },
    )

    catalog = execution_runtimes.execution_runtime_catalog()

    assert all(runtime["checkpoint_restore_supported"] for runtime in catalog["runtimes"])
    assert all(runtime["checkpoint_restore_reason"] is None for runtime in catalog["runtimes"])
    assert [runtime["id"] for runtime in catalog["runtimes"]] == ["vercel"]
    assert catalog["runtimes"][0]["cost"]["billing_basis"] == "at_cost"
    assert "supported optimizer" in catalog["run_recovery_eligibility"]


def test_unavailable_runtime_explains_why_it_cannot_host_restore(monkeypatch) -> None:
    """Carry the deployment failure into runtime and checkpoint-restore capability."""
    monkeypatch.setattr(
        execution_runtimes,
        "protected_vercel_unavailable_reason",
        lambda _settings: "Managed sandbox unavailable",
    )
    monkeypatch.setattr(execution_runtimes.settings, "openrouter_api_key", SecretStr("configured"))

    runtime = execution_runtimes.execution_runtime_catalog()["runtimes"][0]

    assert runtime["available"] is False
    assert runtime["checkpoint_restore_supported"] is False
    assert runtime["checkpoint_restore_reason"] == "Managed sandbox unavailable"
