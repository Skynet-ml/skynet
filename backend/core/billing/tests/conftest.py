"""Shared fixtures for the billing unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.billing import model_dispatch


@pytest.fixture(autouse=True)
def _private_cli_homes(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``HOME`` and ``CODEX_HOME`` at empty folders, so no test copies a real CLI login."""
    home: Path = tmp_path_factory.mktemp("user-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))


@pytest.fixture(autouse=True)
def _no_generation_lookup_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the generation lookup retries but skip their real waits."""
    monkeypatch.setattr(
        model_dispatch, "GENERATION_LOOKUP_DELAYS", (0.0,) * len(model_dispatch.GENERATION_LOOKUP_DELAYS)
    )
