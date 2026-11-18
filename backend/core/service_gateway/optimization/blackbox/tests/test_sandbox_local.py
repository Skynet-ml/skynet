"""Tests for the host-subprocess sandbox runtime used by dev and the test suite."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.config import Settings
from core.exceptions import ServiceError

from ..sandbox import (
    _CREDENTIALS_MISSING,
    AsyncBridgeRuntime,
    LocalSubprocessRuntime,
    LocalSubprocessSession,
    SandboxSpec,
    scorer_runtime_from_settings,
)
from ..sandbox_runtime import BOX_GIT_ENV, _async_spec

_VERCEL_ENV = ("VERCEL_TOKEN", "VERCEL_TEAM_ID", "VERCEL_PROJECT_ID")

try:
    import vercel.sandbox  # noqa: F401

    _SDK_PRESENT = True
except ImportError:
    _SDK_PRESENT = False

_needs_sdk = pytest.mark.skipif(not _SDK_PRESENT, reason="vercel-sandbox SDK not installed")


def _git_reads_safe_bare_repository() -> bool:
    """Return whether a local git exists that knows ``safe.bareRepository`` (git 2.38 or later).

    Returns:
        True when the bare-repository test can run.
    """
    if shutil.which("git") is None:
        return False
    version = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False).stdout
    found = re.search(r"(\d+)\.(\d+)", version)
    return found is not None and (int(found[1]), int(found[2])) >= (2, 38)


_needs_git = pytest.mark.skipif(not _git_reads_safe_bare_repository(), reason="git 2.38 or later not installed")


def _settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> Settings:
    """Build settings with every Vercel variable cleared from the environment first.

    Ignores the developer's ``.env`` file too, so a locally configured
    Vercel token cannot leak into the assertions.

    Args:
        monkeypatch: Pytest fixture.
        **values: Settings fields to set.

    Returns:
        A fresh ``Settings`` instance.
    """
    for name in _VERCEL_ENV:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **values)


@pytest.fixture
def session() -> Iterator[LocalSubprocessSession]:
    """Open a local session with one spec-level environment variable.

    Yields:
        The open session; closed afterwards.
    """
    box = LocalSubprocessRuntime().open(SandboxSpec(lifetime_seconds=60, env={"APP_SPEC": "from-spec"}))
    try:
        yield box
    finally:
        box.close()


def test_local_session_runs_commands_in_a_private_scrubbed_directory(
    session: LocalSubprocessSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files land in a private directory, the env is scrubbed to what the spec and call provide, and exit codes pass through."""
    monkeypatch.setenv("HOST_ONLY", "leak")
    session.write_files({"data/in.txt": "payload"})
    session.write_files(
        {
            "probe.py": "import os, sys\n"
            "print(open('data/in.txt').read())\n"
            "print(os.environ.get('APP_SPEC'), os.environ.get('APP_CALL'), os.environ.get('HOST_ONLY'))\n"
            "print(os.environ['HOME'] == os.getcwd())\n"
            "sys.stderr.write('warned')\n"
            "sys.exit(3)\n",
        }
    )

    result = session.run("python3 probe.py", env={"APP_CALL": "per-call"}, timeout_seconds=30)

    assert result.exit_code == 3
    assert result.timed_out is False
    assert result.stdout.splitlines() == ["payload", "from-spec per-call None", "True"]
    assert result.stderr == "warned"
    assert session.read_file("data/in.txt") == "payload"
    assert session.read_file("missing.txt") is None
    assert Path(session.path).is_dir()


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/hosts", "a/../../escape.txt"])
def test_local_session_refuses_paths_outside_its_directory(session: LocalSubprocessSession, path: str) -> None:
    """Relative escapes and absolute paths are rejected before touching the filesystem.

    Args:
        session: Fixture.
        path: A path that leaves the session directory.
    """
    with pytest.raises(ValueError, match="escapes"):
        session.write_files({path: "x"})
    with pytest.raises(ValueError, match="escapes"):
        session.read_file(path)


def test_local_session_kills_the_whole_process_group_on_timeout(session: LocalSubprocessSession) -> None:
    """A timed-out command is killed together with the children it spawned."""
    session.write_files(
        {
            "hang.py": "import subprocess, sys, time\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
        }
    )
    started = time.monotonic()

    result = session.run("python3 hang.py", timeout_seconds=0.5)

    assert result.timed_out is True
    assert result.exit_code == 137
    assert time.monotonic() - started < 5
    child_pid = int(result.stdout.strip())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the child of the timed-out command is still alive")


def test_local_session_close_removes_the_directory_and_is_idempotent() -> None:
    """``close()`` deletes the working directory; a second call is a no-op."""
    box = LocalSubprocessRuntime().open(SandboxSpec(lifetime_seconds=10))
    path = Path(box.path)
    box.write_files({"f.txt": "x"})
    assert path.is_dir()

    box.close()
    box.close()

    assert not path.exists()


def test_scorer_runtime_refuses_missing_managed_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing managed-sandbox credentials never fall back to the worker host."""
    with pytest.raises(ServiceError) as excinfo:
        scorer_runtime_from_settings(_settings(monkeypatch))

    assert str(excinfo.value) == _CREDENTIALS_MISSING


@_needs_sdk
def test_scorer_runtime_prefers_vercel_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured scorers resolve to the managed Vercel provider through the sync bridge."""
    creds = {"vercel_token": "t", "vercel_team_id": "team", "vercel_project_id": "proj"}

    runtime = scorer_runtime_from_settings(_settings(monkeypatch, **creds))
    assert isinstance(runtime, AsyncBridgeRuntime)
    assert runtime.provider == "vercel"


def test_local_runtime_has_no_header_injecting_edge() -> None:
    """The host runtime cannot inject headers, so callers hand secrets to their commands themselves."""
    assert LocalSubprocessRuntime().injects_headers is False


def test_every_box_spec_refuses_implicit_bare_repositories() -> None:
    """The async provider spec and the local session both carry the box git env (SKY-812)."""
    assert BOX_GIT_ENV == {"GIT_CONFIG_PARAMETERS": "'safe.bareRepository'='explicit'"}
    assert _async_spec(SandboxSpec(lifetime_seconds=60, env={"APP_SPEC": "x"})).env == {
        "APP_SPEC": "x",
        **BOX_GIT_ENV,
    }
    box = LocalSubprocessRuntime().open(SandboxSpec(lifetime_seconds=60, env={"APP_SPEC": "x"}))
    try:
        printed = box.run("printenv GIT_CONFIG_PARAMETERS", timeout_seconds=30).stdout.strip()
        assert printed == BOX_GIT_ENV["GIT_CONFIG_PARAMETERS"]
    finally:
        box.close()


@_needs_git
def test_box_git_reads_no_config_from_a_bare_repository_layout_in_a_tree(session: LocalSubprocessSession) -> None:
    """A tree folder shaped as a bare repository gives box git no config (SKY-812).

    The same command with the box git env cleared runs the planted alias, so
    the layout is a working bare repository and only the env stops it.

    Args:
        session: Fixture.
    """
    marker = Path(session.path) / "marker"
    session.write_files(
        {
            "tree/vendor/HEAD": "ref: refs/heads/main\n",
            "tree/vendor/objects/keep": "",
            "tree/vendor/refs/keep": "",
            "tree/vendor/config": f"[core]\n\tbare = true\n[alias]\n\tpwn = \"!touch '{marker}'\"\n",
        }
    )

    refused = session.run("cd tree/vendor && git rev-parse --git-dir", timeout_seconds=30)
    assert refused.exit_code != 0
    assert "safe.bareRepository" in refused.stderr
    assert session.run("cd tree/vendor && git pwn", timeout_seconds=30).exit_code != 0
    assert not marker.exists()

    no_safety = {"GIT_CONFIG_PARAMETERS": "", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    assert session.run("cd tree/vendor && git pwn", env=no_safety, timeout_seconds=30).exit_code == 0
    assert marker.exists()
