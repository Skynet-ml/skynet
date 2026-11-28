"""The setup check's guest validates a local-folder run that it cannot read (SKY-990)."""

from __future__ import annotations

import pytest

from core.config import settings
from core.service_gateway.optimization.blackbox import preflight

# The guest has no access to the user's computer, so this path does not exist there.
FOLDER = "/Users/someone/projects/app"


def _local_payload(**overrides: object) -> dict[str, object]:
    """Build a run request whose source is a folder on the user's computer.

    Args:
        **overrides: Top-level keys to replace.

    Returns:
        The request, as the parent sends it to the guest.
    """
    payload: dict[str, object] = {
        "name": "local-run",
        "objective": "Make the tests pass.",
        "recipe": "repo",
        "scorer": {"kind": "python", "metric_code": "def score(repo_path, case=None):\n    return 1.0"},
        "strategy": {"mode": "single", "engine": "gepa"},
        "target": {"kind": "repo", "repo": {"provider": "local", "path": FOLDER, "editable_paths": ["."]}},
        "reflection_model_config": {"name": "openai/gpt-4o"},
        "max_cost_cents": 500,
        "_parent_capability": "opaque",
    }
    payload.update(overrides)
    return payload


def test_the_guest_check_accepts_a_local_folder_outside_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guest is not the local stack and cannot see the folder, and the check still passes.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "local_mode", False)

    preflight._validate_optimizer(_local_payload())


def test_the_guest_check_still_refuses_a_local_folder_on_a_remote_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stored context skips only the checks that read this computer, not the provider check.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "local_mode", False)

    with pytest.raises(ValueError, match=r"A local folder runs only on this computer\."):
        preflight._validate_optimizer(_local_payload(sandbox_provider="vercel"))
