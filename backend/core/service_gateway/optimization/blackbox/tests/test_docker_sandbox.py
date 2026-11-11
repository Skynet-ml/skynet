"""Tests for the local Docker availability probe and the local-mode runtime choice."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.config import Settings
from core.i18n import t
from core.worker import engine

from .. import sandbox as sandbox_mod
from ..native_runtime import native_runtime_unavailable_reason
from ..sandbox import (
    _RUNTIME_OVERRIDE,
    AsyncBridgeRuntime,
    docker_sandbox_unavailable_reason,
    sandbox_runtime_from_settings,
    sandbox_unavailable_reason,
    scorer_runtime_from_settings,
)

_IMAGE = "sha256:" + "a" * 64
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
)

# The fake CLI records each call's argv and answers like a healthy daemon,
# unless FAKE_DOCKER_FAIL names a subcommand that should fail.
_FAKE_DOCKER = """#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
command = " ".join(args[:2]) if args[:1] == ["image"] else args[0]
if command in os.environ.get("FAKE_DOCKER_FAIL", "").split(","):
    sys.stderr.write("Error response from daemon: refused\\n")
    sys.exit(1)
print("ok")
"""


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


@pytest.fixture(autouse=True)
def _fresh_probe_cache() -> Iterator[None]:
    """Start and end each test with no cached Docker availability answer."""
    sandbox_mod._probe_cache.clear()
    yield
    sandbox_mod._probe_cache.clear()


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """Install a recording fake Docker CLI.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest fixture.

    Returns:
        The fake CLI's path and the file its calls are logged to.
    """
    binary = tmp_path / "docker"
    binary.write_text(_FAKE_DOCKER.format(python=sys.executable))
    binary.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    log.touch()
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.delenv("FAKE_DOCKER_FAIL", raising=False)
    return str(binary), log


def _calls(log: Path) -> list[list[str]]:
    """Read every recorded fake CLI call.

    Args:
        log: The fake CLI's call log.

    Returns:
        Each call's argv after the binary.
    """
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.mark.parametrize("local_mode", [True, False])
def test_worker_start_removes_idle_sandbox_networks_only_in_local_mode(
    monkeypatch: pytest.MonkeyPatch, local_mode: bool
) -> None:
    """Clear leaked setup networks before the worker can open a box."""
    events: list[str] = []

    class _Worker:
        """Record the start of a stand-in worker."""

        def __init__(self, **kwargs: object) -> None:
            """Accept the worker's settings."""

        def start(self) -> None:
            """Record the start."""
            events.append("start")

        def threads_alive(self) -> bool:
            """Report the stand-in as alive.

            Returns:
                Always ``True``.
            """
            return True

    monkeypatch.setattr(engine, "BackgroundWorker", _Worker)
    monkeypatch.setattr(engine, "_worker", None)
    monkeypatch.setattr(engine.settings, "local_mode", local_mode)
    monkeypatch.setattr(engine, "remove_idle_sandbox_networks", lambda docker_bin: events.append("sweep") or 0)

    engine.get_worker(job_store=None)  # type: ignore[arg-type]

    assert events == (["sweep", "start"] if local_mode else ["start"])


def test_unavailable_reason_explains_a_missing_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Name Docker as the missing piece when its CLI is not installed."""
    settings = _settings(monkeypatch, docker_bin=str(tmp_path / "missing"), local_sandbox_image=_IMAGE)
    assert docker_sandbox_unavailable_reason(settings) == t("sandbox.docker_unavailable")


@pytest.mark.parametrize(
    ("fail", "image", "key"),
    [
        ("info", _IMAGE, "sandbox.docker_unavailable"),
        ("", "", "sandbox.docker_image_missing"),
        ("image inspect", _IMAGE, "sandbox.docker_image_missing"),
    ],
)
def test_unavailable_reason_explains_a_stopped_daemon_or_missing_image(
    fake_docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch, fail: str, image: str, key: str
) -> None:
    """Tell a stopped daemon apart from an image that is not built yet."""
    binary, _ = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_FAIL", fail)
    settings = _settings(monkeypatch, docker_bin=binary, local_sandbox_image=image)
    assert docker_sandbox_unavailable_reason(settings) == t(key)


def test_unavailable_reason_reuses_a_recent_answer(
    fake_docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe the daemon once for questions asked close together, then again after the answer ages."""
    binary, log = fake_docker
    settings = _settings(monkeypatch, docker_bin=binary, local_sandbox_image=_IMAGE)

    assert [docker_sandbox_unavailable_reason(settings) for _ in range(4)] == [None] * 4
    assert [call[0] for call in _calls(log)].count("info") == 1

    monkeypatch.setattr(sandbox_mod, "_PROBE_CACHE_SECONDS", 0.0)
    docker_sandbox_unavailable_reason(settings)
    assert [call[0] for call in _calls(log)].count("info") == 2


def test_local_mode_settings_build_docker_and_never_vercel(
    fake_docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local mode opens async Docker boxes without any Vercel credential configured."""
    binary, _ = fake_docker
    settings = _settings(monkeypatch, local_mode=True, docker_bin=binary, local_sandbox_image=_IMAGE)
    # The package conftest binds a host-subprocess runtime; this test checks
    # the settings resolver itself, so it clears that binding.
    token = _RUNTIME_OVERRIDE.set(None)
    try:
        runtime = sandbox_runtime_from_settings(settings)
        assert isinstance(runtime, AsyncBridgeRuntime)
        assert runtime.provider == "docker"
        assert isinstance(scorer_runtime_from_settings(settings), AsyncBridgeRuntime)
        assert sandbox_unavailable_reason(settings) is None
    finally:
        _RUNTIME_OVERRIDE.reset(token)

    assert native_runtime_unavailable_reason("docker", settings) is None
    assert native_runtime_unavailable_reason("vercel", settings) is None


def test_hosted_settings_refuse_the_docker_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hosted deployment keeps Vercel as its only sandbox, whatever Docker is installed."""
    settings = _settings(monkeypatch, vercel_token="token", vercel_team_id="team", vercel_project_id="project")
    token = _RUNTIME_OVERRIDE.set(None)
    try:
        runtime = sandbox_runtime_from_settings(settings)
    finally:
        _RUNTIME_OVERRIDE.reset(token)
    # A build without the Vercel SDK has no hosted runtime at all, which is also not Docker.
    assert runtime is None or (isinstance(runtime, AsyncBridgeRuntime) and runtime.provider == "vercel")

    assert native_runtime_unavailable_reason("docker", settings) is not None
