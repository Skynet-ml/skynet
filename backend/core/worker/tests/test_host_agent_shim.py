"""Tests for the hidden-path refusals the host-agent shim passes back to the session."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from core.service_gateway.optimization.blackbox import harness_bridge
from core.worker import host_agent_shim

_REFUSALS = [
    {"tool": "Read", "kind": "config"},
    {"tool": "Bash", "kind": "scoring"},
]


def _run_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], refusals: Any
) -> str:
    """Run one shim turn against a fake parent reply and return the shim's stdout.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        tmp_path: Workspace root for the turn.
        capsys: Pytest output capture fixture.
        refusals: The ``refusals`` value of the parent's reply.

    Returns:
        What the shim printed.
    """
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "solver.py").write_text("print('v1')\n", encoding="utf-8")
    archive, _ = host_agent_shim.pack_workspace(workspace)
    reply = {
        "status": "ok",
        "transcript_tail": "Edited solver.py.",
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        "result_tar_b64": base64.b64encode(archive).decode("ascii"),
        "refusals": refusals,
    }
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("APP_PROMPT_FILE", raising=False)
    monkeypatch.setattr(host_agent_shim, "endpoint_url", lambda: "http://parent.invalid/v1/_host_agent")
    monkeypatch.setattr(host_agent_shim, "_post", lambda *_args: (200, reply))
    assert host_agent_shim.main() == 0
    return capsys.readouterr().out


def test_reply_refusals_reach_the_result_line_and_the_parser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reply with refusals gives a result line that carries them, and the bridge reads them back."""
    stdout = _run_turn(monkeypatch, tmp_path, capsys, _REFUSALS)

    (line,) = [json.loads(row) for row in stdout.splitlines() if row.strip()]
    assert line["type"] == "host_agent.result"
    assert line["refusals"] == _REFUSALS
    assert harness_bridge.parse_host_agent_refusals(stdout) == _REFUSALS
    assert harness_bridge.parse_host_agent_output(stdout) == (
        "Edited solver.py.",
        {"input_tokens": 12, "output_tokens": 3},
    )


def test_result_line_keeps_only_the_tool_and_the_path_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Extra fields such as a path or content never leave the shim, and malformed entries drop."""
    reply_refusals = [
        {"tool": "Read", "kind": "scoring", "path": "metric.py", "content": "secret"},
        {"tool": "Read"},
        "Bash",
    ]
    stdout = _run_turn(monkeypatch, tmp_path, capsys, reply_refusals)

    assert "metric.py" not in stdout
    assert "secret" not in stdout
    assert harness_bridge.parse_host_agent_refusals(stdout) == [{"tool": "Read", "kind": "scoring"}]


def test_reply_without_refusals_reports_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A parent reply with no refusal list gives an empty list."""
    stdout = _run_turn(monkeypatch, tmp_path, capsys, None)

    assert harness_bridge.parse_host_agent_refusals(stdout) == []
