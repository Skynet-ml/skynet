"""Tests for the Aider harness: registry entry, launch, protected build and output parsing."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ..harnesses import REGISTRY
from ..harnesses.aider import (
    _SNAPSHOT_UNTRACKED_WORKSPACE,
    AIDER_VERSION,
    SPEC,
)


def test_registry_lists_aider_with_its_pin_and_plain_parser() -> None:
    """Aider registers under its own id with a pinned release and the plain parser."""
    assert REGISTRY["aider"] is SPEC
    assert SPEC.binary == "aider"
    assert SPEC.version == AIDER_VERSION == "0.86.2"
    assert SPEC.output_format == "plain"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_snapshot_commits_a_plain_workspace_once(tmp_path: Path) -> None:
    """A workspace that is not a git repo is committed so Aider can see its files; a repo is left alone."""
    (tmp_path / "task.txt").write_text("files before the run\n")
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}

    for _ in range(2):
        subprocess.run(["sh", "-c", _SNAPSHOT_UNTRACKED_WORKSPACE + "true"], cwd=tmp_path, env=env, check=True)

    log = subprocess.run(
        ["git", "log", "--format=%an <%ae> %s"], cwd=tmp_path, env=env, check=True, capture_output=True, text=True
    )
    assert log.stdout.splitlines() == ["Agent <agent@localhost> Task files before agent run"]
    tracked = subprocess.run(["git", "ls-files"], cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    assert tracked.stdout.split() == ["task.txt"]
