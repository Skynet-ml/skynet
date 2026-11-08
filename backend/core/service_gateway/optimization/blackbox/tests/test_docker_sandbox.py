"""Tests for the local Docker sandbox runtime used in local mode."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from core.config import Settings
from core.exceptions import ServiceError
from core.i18n import t
from core.worker import engine
from core.worker.vercel_dspy import _require_image_protocol

from .. import docker_sandbox
from ..docker_egress_proxy import READY_MARKER
from ..docker_sandbox import (
    DOCKER_JOB_LABEL,
    DockerSandboxRuntime,
    DockerSandboxSession,
    docker_sandbox_unavailable_reason,
    remove_idle_sandbox_networks,
)
from ..native_runtime import native_runtime_unavailable_reason
from ..sandbox import (
    _RUNTIME_OVERRIDE,
    CODEX_REQUIREMENTS_PATH,
    JOB_TAG,
    SandboxSpec,
    check_codex_requirements,
    sandbox_runtime_from_settings,
    sandbox_unavailable_reason,
    scorer_runtime_from_settings,
    write_codex_requirements,
)
from ..sandbox_broker import SandboxBroker

_IMAGE = "sha256:" + "a" * 64
_LIVE_IMAGE = os.environ.get("APP_LOCAL_SANDBOX_IMAGE", "")
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
if "skynet-env" in args:
    pairs = [pair.split("=", 1) for pair in sys.stdin.read().split("\\0") if pair]
    with open(os.environ["FAKE_DOCKER_LOG"] + ".env", "a") as log:
        log.write(json.dumps(dict(pairs)) + "\\n")
command = " ".join(args[:2]) if args[:1] in (["image"], ["network"]) else args[0]
if command in os.environ.get("FAKE_DOCKER_FAIL", "").split(","):
    sys.stderr.write("Error response from daemon: refused\\n")
    sys.exit(1)
if command == "run":
    print("c0ffee" * 10)
elif command == "ps":
    print("box1")
    print("box2")
elif command == "network ls":
    print(os.environ.get("FAKE_DOCKER_NETWORKS", "net1").replace(",", "\\n"))
elif command == "network inspect":
    import time
    created = int(time.time()) - (60 if args[-1].startswith("young") else 3600)
    print(("1" if args[-1].startswith("busy") else "0"), created)
elif command == "inspect":
    print("null")
elif command == "logs":
    print("{ready}")
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
    docker_sandbox._probe_cache.clear()
    yield
    docker_sandbox._probe_cache.clear()


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
    binary.write_text(_FAKE_DOCKER.format(python=sys.executable, ready=READY_MARKER))
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


def _flag_values(argv: list[str], flag: str) -> list[str]:
    """Collect every value passed after ``flag``.

    Args:
        argv: A recorded call.
        flag: The option name.

    Returns:
        The values, in order.
    """
    return [argv[index + 1] for index, item in enumerate(argv) if item == flag]


def test_open_starts_an_offline_read_only_box_labelled_with_its_job(fake_docker: tuple[str, Path]) -> None:
    """Pin the exact isolation flags: no network, no published ports, read-only root, job label."""
    binary, log = fake_docker
    runtime = DockerSandboxRuntime(_IMAGE, docker_bin=binary)

    session = runtime.open(SandboxSpec(lifetime_seconds=90.2, env={"A": "1"}, tags={JOB_TAG: "job-7"}, vcpus=2))

    assert isinstance(session, DockerSandboxSession)
    assert session.container == "c0ffee" * 10
    (argv,) = _calls(log)
    assert argv[:5] == ["run", "-d", "--rm", "--init", "--name"]
    assert _flag_values(argv, "--network") == ["none"]
    assert not {"-p", "--publish", "--publish-all", "-P", "--privileged", "-v", "--volume", "--mount"} & set(argv)
    assert "--read-only" in argv
    assert _flag_values(argv, "--cap-drop") == ["ALL"]
    assert _flag_values(argv, "--security-opt") == ["no-new-privileges"]
    assert _flag_values(argv, "--cpus") == ["2"]
    assert _flag_values(argv, "--memory") == ["4096m"]
    assert _flag_values(argv, "--pids-limit") == ["1024"]
    assert [mount.split(":")[0] for mount in _flag_values(argv, "--tmpfs")] == ["/workspace", "/tmp", "/etc/codex"]
    assert _flag_values(argv, "-w") == ["/workspace"]
    assert _flag_values(argv, "-e") == ["HOME=/workspace", "TMPDIR=/tmp"]
    assert set(_flag_values(argv, "--label")) == {"skynet=blackbox", "skynet_job=job-7", f"{DOCKER_JOB_LABEL}=job-7"}
    assert argv[-4:] == ["--entrypoint", "sleep", _IMAGE, "91"]
    assert runtime.billable is False
    assert runtime.injects_headers is False


def test_open_puts_an_allowlisted_box_behind_its_own_egress_proxy(fake_docker: tuple[str, Path]) -> None:
    """A setup box joins only its own internal network; the proxy alone also joins bridge."""
    binary, log = fake_docker
    runtime = DockerSandboxRuntime(_IMAGE, docker_bin=binary)
    image = "sha256:" + "b" * 64

    session = runtime.open(
        SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org", "github.com"), image=image, tags={JOB_TAG: "job-7"})
    )

    create, proxy, connect, logs, box = _calls(log)
    assert isinstance(session, DockerSandboxSession)
    network = create[-1]
    proxy_name = proxy[proxy.index("--name") + 1]
    assert create[:3] == ["network", "create", "--internal"]
    assert f"{DOCKER_JOB_LABEL}=job-7" in _flag_values(create, "--label")
    assert _flag_values(proxy, "--network") == [network]
    assert {"--read-only", "-I"} <= set(proxy)
    assert _flag_values(proxy, "--cap-drop") == ["ALL"]
    assert _flag_values(proxy, "--user") == ["65534:65534"]
    assert f"{DOCKER_JOB_LABEL}=job-7" in _flag_values(proxy, "--label")
    assert proxy[proxy.index(image) - 2 : proxy.index(image)] == ["--entrypoint", "python3"]
    assert proxy[-4:] == ["3128", "60", "github.com", "pypi.org"]
    assert connect == ["network", "connect", "bridge", proxy_name]
    assert logs == ["logs", proxy_name]
    assert _flag_values(box, "--network") == [network]
    assert "bridge" not in box
    url = f"http://{proxy_name}:3128"
    assert {f"HTTPS_PROXY={url}", f"https_proxy={url}", f"HTTP_PROXY={url}"} <= set(_flag_values(box, "-e"))
    assert {"NO_PROXY=localhost,127.0.0.1,::1", "no_proxy=localhost,127.0.0.1,::1"} <= set(_flag_values(box, "-e"))
    assert box[-2] == image


def test_closing_a_setup_box_removes_it_then_its_proxy_and_network(fake_docker: tuple[str, Path]) -> None:
    """Close removes the box first, because a network with a container attached cannot go."""
    binary, log = fake_docker
    session = DockerSandboxRuntime(_IMAGE, docker_bin=binary).open(
        SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",))
    )
    assert isinstance(session, DockerSandboxSession)
    egress = session.egress
    assert egress is not None
    log.write_text("")

    session.close()
    session.close()

    assert _calls(log) == [
        ["rm", "-f", session.container],
        ["rm", "-f", egress.proxy],
        ["network", "rm", egress.network],
    ]


def test_switching_a_setup_box_offline_drops_its_proxy(fake_docker: tuple[str, Path]) -> None:
    """Once the box is offline its proxy and network go; close does not remove them twice."""
    binary, log = fake_docker
    session = DockerSandboxRuntime(_IMAGE, docker_bin=binary).open(
        SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",))
    )
    assert isinstance(session, DockerSandboxSession)
    egress = session.egress
    assert egress is not None
    log.write_text("")

    session.disable_network()
    session.close()

    inspect = ["inspect", "--format", "{{json .NetworkSettings.Networks}}", session.container]
    assert _calls(log) == [
        inspect,
        inspect,
        ["rm", "-f", egress.proxy],
        ["network", "rm", egress.network],
        ["rm", "-f", session.container],
    ]
    assert session.egress is None


@pytest.mark.parametrize("fail", ["network connect", "run"])
def test_a_failed_egress_or_box_start_removes_the_new_network(
    fake_docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch, fail: str
) -> None:
    """Leave no proxy or network behind when the setup box cannot start."""
    binary, log = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_FAIL", fail)

    with pytest.raises(ServiceError, match="refused"):
        DockerSandboxRuntime(_IMAGE, docker_bin=binary).open(
            SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",))
        )

    calls = _calls(log)
    network = calls[0][-1]
    assert calls[-1] == ["network", "rm", network]


@pytest.mark.parametrize(
    "spec",
    [
        SandboxSpec(lifetime_seconds=60, inject_headers={"api.example.com": {"Authorization": "x"}}),
        SandboxSpec(lifetime_seconds=60, network_disabled=True, allowed_hosts=("pypi.org",)),
        SandboxSpec(lifetime_seconds=0),
        SandboxSpec(lifetime_seconds=float("inf")),
        SandboxSpec(lifetime_seconds=60, vcpus=3),
    ],
)
def test_open_refuses_specs_a_local_box_cannot_honour(fake_docker: tuple[str, Path], spec: SandboxSpec) -> None:
    """Refuse header injection, contradictory networks and out-of-profile bounds before Docker runs."""
    binary, log = fake_docker
    with pytest.raises(ServiceError):
        DockerSandboxRuntime(_IMAGE, docker_bin=binary).open(spec)
    assert _calls(log) == []


def test_open_reports_a_daemon_refusal(fake_docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Carry Docker's own error line into the failure."""
    binary, _ = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_FAIL", "run")
    with pytest.raises(ServiceError, match="refused"):
        DockerSandboxRuntime(_IMAGE, docker_bin=binary).open(SandboxSpec(lifetime_seconds=60))


def test_stop_job_sandboxes_removes_every_box_under_the_job_label(fake_docker: tuple[str, Path]) -> None:
    """Sweep by the ``skynet.job`` label and count each removed box."""
    binary, log = fake_docker

    assert DockerSandboxRuntime(_IMAGE, docker_bin=binary).stop_job_sandboxes("job-7") == 2

    assert _calls(log) == [
        ["ps", "-q", "--filter", f"label={DOCKER_JOB_LABEL}=job-7"],
        ["rm", "-f", "box1"],
        ["rm", "-f", "box2"],
        ["network", "ls", "-q", "--filter", f"label={DOCKER_JOB_LABEL}=job-7"],
        ["network", "rm", "net1"],
    ]


def test_remove_idle_sandbox_networks_keeps_networks_still_in_use(
    fake_docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove only old sandbox networks with no attached container.

    A young idle network may belong to a box that is still opening, so it stays.
    """
    binary, log = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_NETWORKS", "idle1,busy1,young1,idle2")

    assert remove_idle_sandbox_networks(binary) == 2

    inspect = ["network", "inspect", "--format", "{{len .Containers}} {{.Created.Unix}}"]
    assert _calls(log) == [
        ["network", "ls", "-q", "--filter", "label=skynet=blackbox"],
        [*inspect, "idle1"],
        ["network", "rm", "idle1"],
        [*inspect, "busy1"],
        [*inspect, "young1"],
        [*inspect, "idle2"],
        ["network", "rm", "idle2"],
    ]


@pytest.mark.parametrize("fail", ["network ls", "network inspect", "network rm"])
def test_remove_idle_sandbox_networks_logs_failures_and_continues(
    fake_docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch, fail: str
) -> None:
    """A Docker refusal skips that step instead of stopping startup."""
    binary, log = fake_docker
    monkeypatch.setenv("FAKE_DOCKER_NETWORKS", "idle1,idle2")
    monkeypatch.setenv("FAKE_DOCKER_FAIL", fail)

    assert remove_idle_sandbox_networks(binary) == 0
    assert len(_calls(log)) == {"network ls": 1, "network inspect": 3, "network rm": 5}[fail]


def test_remove_idle_sandbox_networks_survives_a_missing_cli(tmp_path: Path) -> None:
    """A missing Docker CLI is logged, not raised."""
    assert remove_idle_sandbox_networks(str(tmp_path / "no-docker")) == 0


@pytest.mark.parametrize("kind", ["directory", "not executable"])
def test_remove_idle_sandbox_networks_survives_a_cli_path_that_cannot_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    """A Docker CLI path that cannot run is logged, not raised, so local startup goes on."""
    docker_bin = tmp_path / "docker"
    if kind == "directory":
        docker_bin.mkdir()
    else:
        docker_bin.write_text("#!/bin/sh\nexit 0\n")
        docker_bin.chmod(0o644)

    with caplog.at_level("WARNING"):
        assert remove_idle_sandbox_networks(str(docker_bin)) == 0

    assert "could not list idle local sandbox networks" in caplog.text


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


def test_session_commands_address_the_container(fake_docker: tuple[str, Path]) -> None:
    """Run, read and close through ``docker exec`` and ``docker rm``; close only once."""
    binary, log = fake_docker
    session = DockerSandboxSession(binary, "box1", env={}, owner=None)

    result = session.run("echo hi", env={"K": "V=1\nnext"}, timeout_seconds=4.5)
    session.read_file("out/a.txt")
    session.close()
    session.close()

    run, read, remove = _calls(log)
    assert result.ok
    timeout = ["timeout", "--signal=KILL", "5s"]
    loader = [*docker_sandbox._SHELL, docker_sandbox._ENV_LOADER, "skynet-env"]
    assert run == [
        "exec",
        "-i",
        "-w",
        "/workspace",
        "box1",
        *loader,
        *timeout,
        "bash",
        "--noprofile",
        "--norc",
        "-c",
        "echo hi",
    ]
    assert json.loads(Path(f"{log}.env").read_text()) == {"K": "V=1\nnext"}
    assert read[:4] == ["exec", "-w", "/workspace", "box1"]
    assert read[-1] == "out/a.txt"
    assert remove == ["rm", "-f", "box1"]


def test_a_root_command_runs_as_the_container_root_user_only_when_asked(fake_docker: tuple[str, Path]) -> None:
    """Only a ``sudo`` command adds ``-u root`` to ``docker exec``; a plain one keeps the image user."""
    binary, log = fake_docker
    session = DockerSandboxSession(binary, "box1", env={}, owner=None)

    session.run("id -u")
    session.run("id -u", sudo=True)
    session.close()

    plain, root, _remove = _calls(log)
    assert "-u" not in plain
    assert root[:6] == ["exec", "-i", "-u", "root", "-w", "/workspace"]


def test_a_planted_shell_profile_never_runs_and_the_env_still_reaches_the_command(
    fake_docker: tuple[str, Path], tmp_path: Path
) -> None:
    """Run the exec argv in a real bash: profiles in ``$HOME`` stay unread, and the env loader still works (E38)."""
    binary, log = fake_docker
    DockerSandboxSession(binary, "box1", env={}, owner=None).run('printf %s "$K"', env={"K": "V=1"})
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "profile-ran"
    for name in (".bash_profile", ".bash_login", ".profile", ".bashrc"):
        (home / name).write_text(f"echo {name} >> {marker}\n")

    shell = subprocess.run(
        _calls(log)[0][5:],
        input=docker_sandbox._env_stream({"K": "V=1"}),
        env={"PATH": os.environ["PATH"], "HOME": str(home), "SSH_CLIENT": "10.0.0.1 1 22"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert shell.stdout == "V=1"
    assert not marker.exists()


def test_box_and_command_secrets_stay_off_every_command_line(fake_docker: tuple[str, Path]) -> None:
    """Send the box's and each command's environment over stdin, so ``ps`` never shows a secret."""
    binary, log = fake_docker
    runtime = DockerSandboxRuntime(_IMAGE, docker_bin=binary)
    session = runtime.open(SandboxSpec(lifetime_seconds=60, env={"APP_API_KEY": "gateway-key", "A": "box"}))

    session.run("true", env={"DOCKER_TOKEN": "scorer-secret", "A": "command"}, timeout_seconds=5)

    start, run = _calls(log)
    assert not any("gateway-key" in arg or "scorer-secret" in arg for arg in start + run)
    assert "-e" not in run
    assert json.loads(Path(f"{log}.env").read_text()) == {
        "APP_API_KEY": "gateway-key",
        "A": "command",
        "DOCKER_TOKEN": "scorer-secret",
    }


def test_session_refuses_a_nul_in_the_environment(fake_docker: tuple[str, Path]) -> None:
    """Refuse an environment that no process can carry, before Docker runs anything."""
    binary, log = fake_docker
    session = DockerSandboxSession(binary, "box1", env={}, owner=None)

    with pytest.raises(ServiceError):
        session.run("true", env={"K": "a\0b"})

    assert _calls(log) == []


@pytest.mark.parametrize("path", ["/etc/passwd", "../escape", ""])
def test_session_refuses_paths_outside_the_workspace(fake_docker: tuple[str, Path], path: str) -> None:
    """Keep file access inside the working directory."""
    binary, _ = fake_docker
    session = DockerSandboxSession(binary, "box1", env={}, owner=None)
    with pytest.raises(ValueError):
        session.write_files({path: "x"})
    with pytest.raises(ValueError):
        session.read_file(path)


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

    monkeypatch.setattr(docker_sandbox, "_PROBE_CACHE_SECONDS", 0.0)
    docker_sandbox_unavailable_reason(settings)
    assert [call[0] for call in _calls(log)].count("info") == 2


def test_local_mode_settings_build_docker_and_never_vercel(
    fake_docker: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local mode opens Docker boxes without any Vercel credential configured."""
    binary, _ = fake_docker
    settings = _settings(monkeypatch, local_mode=True, docker_bin=binary, local_sandbox_image=_IMAGE)
    # The package conftest binds a host-subprocess runtime; this test checks
    # the settings resolver itself, so it clears that binding.
    token = _RUNTIME_OVERRIDE.set(None)
    try:
        assert isinstance(sandbox_runtime_from_settings(settings), DockerSandboxRuntime)
        assert isinstance(scorer_runtime_from_settings(settings), DockerSandboxRuntime)
    finally:
        _RUNTIME_OVERRIDE.reset(token)

    assert sandbox_unavailable_reason(settings) is None
    assert native_runtime_unavailable_reason("docker", settings) is None
    assert native_runtime_unavailable_reason("vercel", settings) is None


def test_hosted_settings_refuse_the_docker_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hosted deployment keeps Vercel as its only sandbox, whatever Docker is installed."""
    settings = _settings(monkeypatch, vercel_token="token", vercel_team_id="team", vercel_project_id="project")
    token = _RUNTIME_OVERRIDE.set(None)
    try:
        assert not isinstance(sandbox_runtime_from_settings(settings), DockerSandboxRuntime)
    finally:
        _RUNTIME_OVERRIDE.reset(token)

    assert native_runtime_unavailable_reason("docker", settings) is not None


def test_broker_accepts_a_bare_local_image_without_a_vercel_quote(fake_docker: tuple[str, Path]) -> None:
    """The broker skips the Vercel price check for a free local runtime and its bare image id."""
    binary, _ = fake_docker
    broker = SandboxBroker(DockerSandboxRuntime(_IMAGE, docker_bin=binary), image=_IMAGE, max_lifetime_seconds=60)
    broker.close()


# --- Live Docker -----------------------------------------------------------

_live = pytest.mark.skipif(
    shutil.which("docker") is None or not _LIVE_IMAGE,
    reason="needs a Docker CLI and APP_LOCAL_SANDBOX_IMAGE set to a built local sandbox image",
)


def _container_exists(container: str) -> bool:
    """Check whether Docker still knows ``container``.

    Args:
        container: Container id.

    Returns:
        True while the container exists.
    """
    return subprocess.run(["docker", "inspect", container], capture_output=True, check=False).returncode == 0


@pytest.fixture
def live_session() -> Iterator[DockerSandboxSession]:
    """Open one real box on the local sandbox image and remove it afterwards.

    Yields:
        The open session.
    """
    session = DockerSandboxRuntime(_LIVE_IMAGE).open(
        SandboxSpec(lifetime_seconds=300, name="skynet-test", tags={JOB_TAG: "sky113-test"})
    )
    assert isinstance(session, DockerSandboxSession)
    try:
        yield session
    finally:
        session.close()


@pytest.mark.docker
@_live
def test_live_box_writes_reads_and_streams(live_session: DockerSandboxSession) -> None:
    """Write files, stream output while the command runs, and read results back."""
    live_session.write_files({"src/a.txt": "alpha\n", "b.txt": "beta"})
    arrivals: list[tuple[str, float]] = []

    result = live_session.run(
        "cat src/a.txt; sleep 2; echo late >&2; echo done > out.txt",
        timeout_seconds=30,
        on_output=lambda stream, line: arrivals.append((line, time.monotonic())),
    )

    assert result.ok
    assert result.stdout == "alpha\n"
    assert result.stderr == "late\n"
    assert arrivals[-1][1] - arrivals[0][1] >= 1.5
    assert live_session.read_file("out.txt") == "done\n"
    assert live_session.read_file("b.txt") == "beta"
    assert live_session.read_file("missing.txt") is None


@pytest.mark.docker
@_live
def test_live_box_and_command_environment_reach_the_command() -> None:
    """Merge the box's and the command's environment inside the box, newlines and all."""
    session = DockerSandboxRuntime(_LIVE_IMAGE).open(
        SandboxSpec(lifetime_seconds=120, env={"BOX": "box", "SHARED": "box"}, tags={JOB_TAG: "sky113-test"})
    )
    try:
        result = session.run(
            'printf "%s|%s|%s|%s|%s|" "$BOX" "$SHARED" "$MULTI" "$HOME" "$pair"; env | grep -c "^A.B=1$"',
            env={"SHARED": "command", "MULTI": "a=b\nc d", "pair": "kept", "A.B": "1"},
            timeout_seconds=30,
        )
    finally:
        session.close()

    assert result.ok
    assert result.stdout == "box|command|a=b\nc d|/workspace|kept|1\n"
    assert result.stderr == ""


@pytest.mark.docker
@_live
def test_live_box_files_can_be_written_while_a_command_runs(live_session: DockerSandboxSession) -> None:
    """A mailbox answer lands while the guest still waits for it."""
    waiter = threading.Thread(
        target=lambda: arrivals.append(
            live_session.run(
                "for i in $(seq 50); do test -f reply && break; sleep 0.2; done; cat reply", timeout_seconds=30
            )
        )
    )
    arrivals: list[Any] = []
    waiter.start()
    time.sleep(1)
    live_session.write_files({"reply": "answer"})
    waiter.join()
    assert arrivals[0].stdout == "answer"


@pytest.mark.docker
@_live
def test_live_box_has_no_network_and_a_read_only_root(live_session: DockerSandboxSession) -> None:
    """Show the isolation: outbound requests fail and only the workspace and /tmp are writable."""
    curl = live_session.run("curl -sS --max-time 5 https://example.com || exit 7", timeout_seconds=30)
    python = live_session.run(
        "python3 -c \"import urllib.request; urllib.request.urlopen('http://1.1.1.1', timeout=5)\"",
        timeout_seconds=30,
    )
    writes = live_session.run(
        "touch /workspace/ok /tmp/ok && ! touch /app/blocked 2>/dev/null && ! touch /etc/blocked 2>/dev/null",
        timeout_seconds=30,
    )
    identity = live_session.run("echo $HOME; cat /proc/self/status | grep CapEff", timeout_seconds=30)

    assert curl.exit_code != 0
    assert not python.ok
    assert writes.ok, writes.stderr
    assert identity.stdout.splitlines()[0] == "/workspace"
    assert identity.stdout.split()[-1] == "0000000000000000"


@pytest.mark.docker
@_live
def test_live_read_only_box_takes_the_root_owned_codex_requirements(live_session: DockerSandboxSession) -> None:
    """Write the Codex requirements as root in a real read-only box; the box user cannot change them (SKY-389)."""
    write_codex_requirements(live_session)
    check_codex_requirements(live_session)
    tamper = live_session.run(
        f"echo x >> {CODEX_REQUIREMENTS_PATH} || rm -f {CODEX_REQUIREMENTS_PATH} || touch /etc/codex/other; "
        f"test -f /etc/codex/other && exit 9; id -u",
        timeout_seconds=30,
    )

    assert tamper.stdout.strip().splitlines()[-1] != "0"
    assert tamper.exit_code != 9
    check_codex_requirements(live_session)


@pytest.mark.docker
@_live
def test_live_box_times_out_and_close_removes_the_container(live_session: DockerSandboxSession) -> None:
    """Kill a command at its timeout, and remove the container on close."""
    result = live_session.run("sleep 30", timeout_seconds=1)
    assert result.timed_out

    live_session.close()
    assert not _container_exists(live_session.container)


@pytest.mark.docker
@_live
def test_live_image_speaks_this_workers_protocol(live_session: DockerSandboxSession) -> None:
    """The DSPy image check passes against the locally built image."""
    _require_image_protocol(live_session, 300)


@pytest.mark.docker
@_live
def test_live_stop_job_sandboxes_sweeps_leftover_boxes() -> None:
    """Remove a box the run never closed, by its job label."""
    runtime = DockerSandboxRuntime(_LIVE_IMAGE)
    session = runtime.open(SandboxSpec(lifetime_seconds=120, tags={JOB_TAG: "sky113-sweep"}))
    assert isinstance(session, DockerSandboxSession)

    assert runtime.stop_job_sandboxes("sky113-sweep") == 1
    assert not _container_exists(session.container)


def _network_exists(network: str) -> bool:
    """Check whether Docker still knows ``network``.

    Args:
        network: Network name.

    Returns:
        True while the network exists.
    """
    return subprocess.run(["docker", "network", "inspect", network], capture_output=True, check=False).returncode == 0


# Docker Desktop for Mac routes this name and address to the Mac itself,
# including services that listen only on the Mac's loopback.
_MAC_HOST_TARGETS = ("host.docker.internal", "192.168.65.254")

_HOST_PROBE = """
import socket, sys, urllib.request
ports = [int(port) for port in sys.argv[1:]]
for host in {targets!r}:
    for port in ports:
        for label, attempt in (
            ("direct", lambda: socket.create_connection((host, port), 4).close()),
            ("proxy", lambda: urllib.request.urlopen(f"http://{{host}}:{{port}}/", timeout=8).read()),
        ):
            try:
                attempt()
                print("REACHED", label, host, port)
            except Exception as error:
                print("BLOCKED", label, host, port, type(error).__name__)
"""


@pytest.fixture
def mac_listeners() -> Iterator[tuple[list[int], list[int]]]:
    """Listen on this Mac's loopback only and on every interface, counting connections.

    Yields:
        The two ports and a list that grows by one per accepted connection.
    """
    accepted: list[int] = []
    servers = [socket.create_server(("127.0.0.1", 0)), socket.create_server(("0.0.0.0", 0))]

    def accept(server: socket.socket) -> None:
        """Count and answer every connection until the server closes.

        Args:
            server: The listening socket.
        """
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            accepted.append(1)
            with connection:
                connection.sendall(b"HTTP/1.0 200 OK\r\n\r\nmac-secret")

    for server in servers:
        threading.Thread(target=accept, args=(server,), daemon=True).start()
    try:
        yield [server.getsockname()[1] for server in servers], accepted
    finally:
        for server in servers:
            server.close()


@pytest.fixture
def live_setup_session() -> Iterator[DockerSandboxSession]:
    """Open one real allowlisted setup box and remove it, its proxy and its network afterwards.

    Yields:
        The open session.
    """
    session = DockerSandboxRuntime(_LIVE_IMAGE).open(
        SandboxSpec(
            lifetime_seconds=300,
            name="skynet-test-setup",
            allowed_hosts=PACKAGE_REGISTRY_HOSTS,
            tags={JOB_TAG: "sky113-setup"},
        )
    )
    assert isinstance(session, DockerSandboxSession)
    try:
        yield session
    finally:
        session.close()


@pytest.mark.docker
@_live
def test_live_setup_box_cannot_reach_services_on_this_mac(
    live_setup_session: DockerSandboxSession, mac_listeners: tuple[list[int], list[int]]
) -> None:
    """Neither a loopback-only nor an every-interface Mac listener answers a setup box, directly or via the proxy."""
    ports, accepted = mac_listeners
    probe = _HOST_PROBE.format(targets=_MAC_HOST_TARGETS)
    # Control: a plain bridge container does reach the loopback-only
    # listener, so the probe can see the leak this runtime must close.
    control = subprocess.run(
        ["docker", "run", "--rm", "--network", "bridge", "--entrypoint", "python3", _LIVE_IMAGE, "-c", probe]
        + [str(port) for port in ports],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert f"REACHED direct host.docker.internal {ports[0]}" in control.stdout, control.stdout + control.stderr
    accepted.clear()

    live_setup_session.write_files({"probe.py": probe})
    result = live_setup_session.run("python3 probe.py " + " ".join(map(str, ports)), timeout_seconds=120)

    lines = result.stdout.splitlines()
    assert len(lines) == len(_MAC_HOST_TARGETS) * len(ports) * 2, result.stdout + result.stderr
    assert all(line.startswith("BLOCKED") for line in lines), result.stdout
    assert accepted == []


@pytest.mark.docker
@_live
def test_live_setup_box_reaches_registries_only(live_setup_session: DockerSandboxSession) -> None:
    """A registry answers through the proxy; another host and any direct route are refused."""
    registry = live_setup_session.run(
        "curl -sS -o /dev/null -w '%{http_code}' --max-time 60 https://pypi.org/simple/six/", timeout_seconds=90
    )
    pip = live_setup_session.run(
        "python3 -m pip download --no-deps --no-cache-dir -q -d /tmp/dl six==1.16.0 && ls /tmp/dl", timeout_seconds=180
    )
    other = live_setup_session.run("curl -sS --max-time 20 https://example.com/", timeout_seconds=60)
    direct = live_setup_session.run(
        "curl -sS --noproxy '*' --max-time 10 https://pypi.org/simple/six/ || exit 7", timeout_seconds=60
    )
    raw_ip = live_setup_session.run("curl -sS --max-time 20 https://1.1.1.1/", timeout_seconds=60)

    assert registry.stdout == "200", registry.stderr
    assert pip.ok, pip.stderr
    assert "six-1.16.0" in pip.stdout
    assert other.exit_code != 0
    assert "403" in other.stderr
    assert direct.exit_code != 0
    assert raw_ip.exit_code != 0
    assert "403" in raw_ip.stderr


@pytest.mark.docker
@_live
def test_live_setup_box_close_and_sweep_remove_its_proxy_and_network() -> None:
    """Close removes the box, its proxy and its network; the job sweep does too for a box left open."""
    runtime = DockerSandboxRuntime(_LIVE_IMAGE)
    spec = SandboxSpec(lifetime_seconds=120, allowed_hosts=("pypi.org",), tags={JOB_TAG: "sky113-egress-sweep"})
    closed = runtime.open(spec)
    left = runtime.open(spec)
    assert isinstance(closed, DockerSandboxSession)
    assert isinstance(left, DockerSandboxSession)
    assert closed.egress is not None
    assert left.egress is not None
    closed_egress, left_egress = closed.egress, left.egress

    closed.close()
    assert not _container_exists(closed.container)
    assert not _container_exists(closed_egress.proxy)
    assert not _network_exists(closed_egress.network)

    assert runtime.stop_job_sandboxes("sky113-egress-sweep") == 2
    assert not _container_exists(left.container)
    assert not _container_exists(left_egress.proxy)
    assert not _network_exists(left_egress.network)


@pytest.mark.docker
@_live
def test_live_setup_box_goes_offline_and_drops_its_proxy(live_setup_session: DockerSandboxSession) -> None:
    """After the switch the box reaches nothing, and its proxy and network are gone."""
    egress = live_setup_session.egress
    assert egress is not None

    live_setup_session.disable_network()
    result = live_setup_session.run("curl -sS --max-time 10 https://pypi.org/simple/six/ || exit 7", timeout_seconds=60)

    assert result.exit_code != 0
    assert not _container_exists(egress.proxy)
    assert not _network_exists(egress.network)


@pytest.mark.docker
@_live
def test_live_setup_box_reaches_loopback_after_going_offline(live_setup_session: DockerSandboxSession) -> None:
    """The proxy variables stay in the box, so a scorer's loopback server must still answer after the switch."""
    live_setup_session.disable_network()
    script = (
        "python3 -m http.server 8765 --bind 127.0.0.1 >/dev/null 2>&1 & sleep 1; "
        "python3 -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/').status)\""
    )

    result = live_setup_session.run(script, timeout_seconds=60)

    assert result.ok, result.stderr
    assert result.stdout.strip() == "200"
