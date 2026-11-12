"""Verify the guest refuses a request its image's protocol cannot read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.worker import isolated_runner


def test_guest_refuses_an_unknown_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Stop before authored code and ask for a rebuilt image.

    Args:
        tmp_path: Guest request staging directory.
        monkeypatch: Runner and argument substitutions.
        capfd: Captures the framed event the guest writes to its real stdout.
    """
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {"payload": {}, "artifact_id": "g1", "nonce": "fixture", "protocol": isolated_runner.SANDBOX_PROTOCOL + 1}
        )
    )

    def unexpected(*_args: Any, **_kwargs: Any) -> None:
        """Fail if authored code runs on an image that cannot read the request."""
        raise AssertionError("the guest ran the optimizer on an incompatible image")

    monkeypatch.setattr(isolated_runner, "run_service_in_subprocess", unexpected)
    monkeypatch.setattr(isolated_runner.sys, "argv", ["isolated_runner", str(request)])
    isolated_runner.main()

    line = capfd.readouterr().out.strip()
    prefix = f"{isolated_runner.EVENT_PREFIX}fixture "
    assert line.startswith(prefix)
    event = json.loads(line[len(prefix) :])
    assert event == {"type": "error", "error": isolated_runner.INCOMPATIBLE_IMAGE_MESSAGE, "traceback": ""}


def test_guest_ignores_worker_dependency_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the optimizer whatever the worker's own Python and packages are.

    Args:
        tmp_path: Guest request staging directory.
        monkeypatch: Runner and argument substitutions.
    """
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "payload": {},
                "artifact_id": "g1",
                "nonce": "fixture",
                "protocol": isolated_runner.SANDBOX_PROTOCOL,
                "runtime_identity": {"python": "0.0.0", "dependencies": {"litellm": "0.0.0"}},
            }
        )
    )
    observed: list[str] = []

    def run(payload: dict[str, Any], artifact_id: str, events: Any) -> None:
        """Record that the optimizer entry point was reached."""
        observed.append(artifact_id)

    monkeypatch.setattr(isolated_runner, "run_service_in_subprocess", run)
    monkeypatch.setattr(isolated_runner.sys, "argv", ["isolated_runner", str(request)])
    isolated_runner.main()
    assert observed == ["g1"]
