"""Check that the emulator guard removes service endpoint overrides."""

from __future__ import annotations

import os

import pytest

from tests.aws_emulator.conftest import ENDPOINT, connect

# Nothing listens on port 1, so a call that uses this endpoint fails.
CLOSED = "https://127.0.0.1:1"


def test_connect_removes_service_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a stray service endpoint cannot move the STS check off the emulator."""
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", CLOSED)
    monkeypatch.setenv("AWS_ENDPOINT_URL_SECRETS_MANAGER", CLOSED)
    monkeypatch.setenv("AWS_IGNORE_CONFIGURED_ENDPOINT_URLS", "true")

    assert connect(monkeypatch).endpoint == ENDPOINT
    assert not [name for name in os.environ if name.startswith("AWS_ENDPOINT_URL_")]
    assert "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS" not in os.environ
