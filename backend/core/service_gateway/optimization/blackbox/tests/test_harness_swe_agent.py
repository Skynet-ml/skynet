"""Tests for the SWE-agent harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_SWE_AGENT, BLACKBOX_HARNESSES

from ..harnesses import REGISTRY
from ..harnesses.swe_agent import (
    _PROBE,
    _SNAPSHOT_WORKSPACE,
    SWE_AGENT_COMMIT,
    SWE_AGENT_EFFORTS,
    SWE_AGENT_VERSION,
)

_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_INSTALL_DIR = Path(".local/share/swe-agent")


def _host_env(path: str) -> dict[str, str]:
    """Return a minimal environment with ``path`` first on PATH.

    The macOS git shim needs ``DEVELOPER_DIR`` when it is set.

    Args:
        path: Folder to put first on PATH.

    Returns:
        The environment.
    """
    env = {"PATH": f"{path}{os.pathsep}{os.environ.get('PATH', os.defpath)}", "GIT_CONFIG_NOSYSTEM": "1"}
    if "DEVELOPER_DIR" in os.environ:
        env["DEVELOPER_DIR"] = os.environ["DEVELOPER_DIR"]
    return env


def test_swe_agent_is_registered_after_mini_swe_agent_with_its_pin() -> None:
    """List swe_agent in the registry and the request model, pinned to the probe and release."""
    spec = REGISTRY[BLACKBOX_HARNESS_SWE_AGENT]

    assert BLACKBOX_HARNESS_SWE_AGENT == "swe_agent"
    assert BLACKBOX_HARNESS_SWE_AGENT in BLACKBOX_HARNESSES
    ids = list(REGISTRY)
    assert ids.index("mini_swe_agent") + 1 == ids.index("swe_agent")
    assert (spec.binary, spec.version, spec.output_format) == (_PROBE, "1.1.0", "plain")
    assert SWE_AGENT_COMMIT == "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_snapshot_commits_an_earlier_runs_edits_so_the_patch_holds_only_this_run(tmp_path: Path) -> None:
    """Commit a resumed workspace's earlier edits, so ``git diff`` starts empty."""
    env = _host_env(str(tmp_path))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@localhost"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=env, check=True)
    (tmp_path / "a.txt").write_text("old\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, env=env, check=True)
    subprocess.run([*git, "commit", "-qm", "base"], cwd=tmp_path, env=env, check=True)
    (tmp_path / "a.txt").write_text("run one\n")
    (tmp_path / "new.txt").write_text("run one\n")

    subprocess.run(
        [shutil.which("bash") or "/bin/sh", "-c", _SNAPSHOT_WORKSPACE], cwd=tmp_path, env=env, check=True, timeout=30
    )

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, env=env, capture_output=True, text=True, check=True
    )
    assert status.stdout == ""


def _snapshot(cwd: Path, env: dict[str, str]) -> None:
    """Run the real snapshot shell string in ``cwd``.

    Args:
        cwd: Workspace folder.
        env: Environment for the shell.
    """
    subprocess.run(
        [shutil.which("bash") or "/bin/sh", "-c", _SNAPSHOT_WORKSPACE], cwd=cwd, env=env, check=True, timeout=30
    )


def _git_out(cwd: Path, env: dict[str, str], *args: str) -> str:
    """Return the stdout of one git command in ``cwd``.

    Args:
        cwd: Folder to run git in.
        env: Environment for git.
        *args: Git arguments.

    Returns:
        The command's stdout.
    """
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_snapshot_bounds_git_add_with_timeout_when_timeout_is_present(tmp_path: Path) -> None:
    """Run ``git add -A`` through ``timeout 120`` when the image has ``timeout``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "timeout-args"
    fake = bin_dir / "timeout"
    fake.write_text(f'#!/bin/sh\necho "$@" > {shlex.quote(str(record))}\nshift\nexec "$@"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("old\n")
    env = _host_env(str(bin_dir))

    _snapshot(workspace, env)

    assert record.read_text().strip() == "120 git add -A"
    assert _git_out(workspace, env, "ls-files").split() == ["a.txt"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_snapshot_runs_git_add_unbounded_when_timeout_is_missing(tmp_path: Path) -> None:
    """Still snapshot the workspace on an image without ``timeout``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(shutil.which("git") or "git")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("old\n")
    env = {**_host_env(str(bin_dir)), "PATH": str(bin_dir)}
    probe = subprocess.run(
        [shutil.which("bash") or "/bin/sh", "-c", "command -v timeout"],
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert probe.returncode != 0

    _snapshot(workspace, env)

    assert _git_out(workspace, env, "ls-files").split() == ["a.txt"]
    assert _git_out(workspace, env, "status", "--porcelain") == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_snapshot_ignores_a_failing_commit_hook_and_gpg_signing(tmp_path: Path) -> None:
    """Commit the snapshot when a ``prepare-commit-msg`` hook fails and signing has no gpg."""
    env = _host_env(str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=env, check=True)
    hook = tmp_path / ".git" / "hooks" / "prepare-commit-msg"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    subprocess.run(["git", "config", "commit.gpgsign", "true"], cwd=tmp_path, env=env, check=True)
    subprocess.run(["git", "config", "gpg.program", str(tmp_path / "no-gpg")], cwd=tmp_path, env=env, check=True)
    (tmp_path / "a.txt").write_text("old\n")

    _snapshot(tmp_path, env)

    assert _git_out(tmp_path, env, "log", "--format=%s") == "Task files before agent run\n"
    assert _git_out(tmp_path, env, "status", "--porcelain") == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_snapshot_gives_a_workspace_inside_an_outer_repo_its_own_repo(tmp_path: Path) -> None:
    """Keep the outer repo's files out of the snapshot, so the patch paths start at the workspace."""
    env = _host_env(str(tmp_path))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=env, check=True)
    (tmp_path / "outer.txt").write_text("outer\n")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("old\n")

    _snapshot(workspace, env)

    assert Path(_git_out(workspace, env, "rev-parse", "--show-toplevel").strip()) == workspace.resolve()
    assert _git_out(workspace, env, "ls-files").split() == ["a.txt"]
    (workspace / "a.txt").write_text("fixed\n")
    assert "diff --git a/a.txt b/a.txt" in _git_out(workspace, env, "diff")


def test_probe_prints_the_release_only_for_the_pinned_litellm_and_swe_rex(tmp_path: Path) -> None:
    """Run the probe in a venv with fake package metadata for the pinned and drifted versions."""
    venv = tmp_path / _INSTALL_DIR / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=60)
    site = next(venv.glob("lib/python*/site-packages"))

    def install(name: str, version: str) -> None:
        """Write a fake dist-info so the probe sees the package at this version."""
        dist = site / f"{name.replace('-', '_')}-{version}.dist-info"
        dist.mkdir(exist_ok=True)
        (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")

    def probe() -> str:
        """Run the version probe script in a clean env and return its output."""
        result = subprocess.run(
            [shutil.which("bash") or "/bin/sh", "-c", _PROBE],
            env={"PATH": os.defpath, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return result.stdout.strip()

    install("sweagent", SWE_AGENT_VERSION)
    install("swe-rex", "1.4.0")
    install("litellm", "1.103.1")
    assert probe() == SWE_AGENT_VERSION
    for dist in site.glob("litellm-*.dist-info"):
        shutil.rmtree(dist)
    install("litellm", "1.0.0")
    assert probe() == "litellm 1.0.0 swe-rex 1.4.0"


def test_registry_declares_the_litellm_effort_levels() -> None:
    """Declare the LiteLLM levels, so submit refuses any other level before paid work."""
    assert REGISTRY[BLACKBOX_HARNESS_SWE_AGENT].supported_efforts == SWE_AGENT_EFFORTS == _EFFORTS
