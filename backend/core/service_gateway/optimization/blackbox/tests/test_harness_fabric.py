"""Tests for the NeMo Fabric harness: registry entry, pinned install, gateway-only config, protected env and parsing."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_FABRIC, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import parse_output, parse_plain_output
from ..harnesses import REGISTRY
from ..harnesses.fabric import (
    FABRIC_ADAPTER,
    FABRIC_CONFIG_FILE,
    FABRIC_PACKAGES,
    FABRIC_UV_SHA256,
    FABRIC_VERSION,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="gateway-token")
_RELAY = "http://127.0.0.1:3210/v1"
_ANSWER = "I read solution.py and fixed the off-by-one error.\n\nThe answer is 42."
# Synthetic result file in the shape ``nemo_fabric.integrations.harbor.runner``
# writes on success (``FabricRunResult.to_mapping()``); no real run produced it.
_RECORDED_RESULT = {
    "runtime_id": "run-0001",
    "request_id": "req-0001",
    "status": "succeeded",
    "error": None,
    "output": {"response": _ANSWER + "\n"},
    "artifacts": {"root": "/tmp/app-fabric-x/artifacts", "artifacts": []},
}
# The runner's failure shape: it catches the error and exits 1.
_RECORDED_FAILURE = {
    "runner_error": {"stage": "run", "code": "harness_failed", "message": "codex exited 2", "retryable": False}
}
# Stand-in for ``nemo_fabric.integrations.harbor.runner``: it records the spec,
# its argv and the key env it saw, then writes the result the test chose.
_FAKE_RUNNER = """\
import argparse, json, os, sys
parser = argparse.ArgumentParser()
parser.add_argument("--spec")
parser.add_argument("--result")
args = parser.parse_args()
names = ("MODEL_API_KEY", "OPENAI_API_KEY", "APP_API_KEY")
seen = {
    "spec": json.load(open(args.spec)),
    "isolated": sys.flags.isolated,
    "env": {name: os.environ.get(name) for name in names},
}
json.dump(seen, open(os.environ["FAKE_RUNNER_LOG"], "w"))
print("runner progress line")
result = json.loads(os.environ["FAKE_RESULT"])
open(args.result, "w").write(json.dumps(result))
sys.exit(0 if result.get("status") == "succeeded" else 1)
"""
# Stand-in for uv: ``venv`` makes a real stdlib venv, ``pip install`` copies the
# fake Fabric package into it and logs the requested pins. No network.
_FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_UV_LOG"
if [ "$1" = "venv" ]; then exec "$REAL_PYTHON" -m venv --without-pip "$4"; fi
if [ "$1" = "pip" ]; then
  exec "$4" -c 'import shutil, sys, sysconfig; shutil.copytree(sys.argv[1], sysconfig.get_paths()["purelib"], dirs_exist_ok=True)' "$FAKE_PACKAGE"
fi
exit 2
"""


def _target() -> BlackboxTarget:
    """Build a Fabric agent target.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=BLACKBOX_HARNESS_FABRIC, model="target-model")


def _fake_package(root: Path) -> Path:
    """Write a fake ``nemo_fabric`` package with ``nemo-fabric-runtime`` 0.5.0 metadata.

    Args:
        root: Directory to hold the package tree.

    Returns:
        The directory to copy into a venv's site-packages.
    """
    package = root / "fake-package"
    harbor = package / "nemo_fabric" / "integrations" / "harbor"
    harbor.mkdir(parents=True)
    for directory in (package / "nemo_fabric", package / "nemo_fabric" / "integrations", harbor):
        (directory / "__init__.py").write_text("")
    (harbor / "runner.py").write_text(_FAKE_RUNNER)
    dist = package / f"nemo_fabric_runtime-{FABRIC_VERSION}.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: nemo-fabric-runtime\nVersion: {FABRIC_VERSION}\n")
    return package


def _install(tmp_path: Path, launch_install: str) -> tuple[dict[str, str], Path]:
    """Run the install command with fake uv and no ``curl``, so it builds a local stand-in Fabric.

    Args:
        tmp_path: Scratch directory for the home, the fakes and their logs.
        launch_install: The install command under test.

    Returns:
        The env that later commands share, and the fake uv call log.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(_FAKE_UV)
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    uv_log = tmp_path / "uv.log"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{os.defpath}",
        "HOME": str(tmp_path / "home"),
        "REAL_PYTHON": sys.executable,
        "FAKE_PACKAGE": str(_fake_package(tmp_path)),
        "FAKE_UV_LOG": str(uv_log),
    }
    subprocess.run(
        launch_install, shell=True, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=True
    )
    return env, uv_log


def _run(tmp_path: Path, *, protected: bool, result: dict) -> tuple[subprocess.CompletedProcess[str], dict, Path]:
    """Install the stand-in Fabric, then run the built command in a scratch workspace.

    Args:
        tmp_path: Scratch directory for the workspace, the fakes, their logs and
            the run's ``TMPDIR`` (``tmp_path / "tmp"``).
        protected: Build the protected launch, with the sandbox-local relay set.
        result: The result file the fake runner writes.

    Returns:
        The finished run command, what the fake runner saw, and the workspace.
    """
    env, _ = _install(tmp_path, build_launch(_target(), _GATEWAY).install_command or "")
    launch = build_launch(_target(), _GATEWAY, protected=protected)
    workspace = tmp_path / "work"
    for name, content in launch.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (workspace / "task").mkdir(parents=True)
    (workspace / "task" / "PROMPT.md").write_text("Fix `solution.py` and say $(echo done).")
    runner_log = tmp_path / "runner.json"
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    env = {
        **env,
        **launch.env,
        "OPENAI_API_KEY": "leaked-provider-key",
        "FAKE_RUNNER_LOG": str(runner_log),
        "FAKE_RESULT": json.dumps(result),
        "TMPDIR": str(tmp_dir),
    }
    if protected:
        env["APP_BUDGET_RELAY_URL"] = _RELAY
    finished = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    return finished, json.loads(runner_log.read_text()), workspace


def test_fabric_is_registered_at_position_61_before_cline_with_its_pin() -> None:
    """List Fabric at position 61 in the registry and in the request model, pinned to the exact runtime release."""
    spec = REGISTRY[BLACKBOX_HARNESS_FABRIC]
    assert (spec.binary, spec.version, spec.output_format) == ("fabric", "0.5.0", "plain")
    assert spec.position == 61
    order = list(REGISTRY)
    # Relative order only, so later harnesses can join the registry without breaking this test.
    assert order.index("prime") < order.index(BLACKBOX_HARNESS_FABRIC) < order.index("cline")
    assert BLACKBOX_HARNESS_FABRIC in BLACKBOX_HARNESSES
    assert (
        pinned_harness_check(BLACKBOX_HARNESS_FABRIC) == "fabric --version 2>/dev/null | grep -Eq '(^| )0[.]5[.]0( |$)'"
    )


def test_open_launch_pins_the_packages_and_sends_model_calls_only_to_the_gateway() -> None:
    """Pin every package, put the gateway URL in the config file, and keep the key and provider keys out of files."""
    launch = build_launch(_target(), _GATEWAY)

    assert FABRIC_PACKAGES == ("nemo-fabric-runtime==0.5.0", "nemo-fabric-adapters-codex[harness]==0.5.0")
    assert launch.install_command is not None
    assert "nemo-fabric-runtime==0.5.0 'nemo-fabric-adapters-codex[harness]==0.5.0' >" in launch.install_command
    assert "https://github.com/astral-sh/uv/releases/download/0.13.7/" in launch.install_command
    assert "install.sh" not in launch.install_command
    assert "| sh;" not in launch.install_command
    assert launch.install_command.index("sha256sum -c -") < launch.install_command.index("tar -xzf")
    assert "uv venv --python 3.12" in launch.install_command
    assert "harbor" not in launch.install_command
    assert launch.install_command.endswith("&& fabric --version")
    assert set(launch.files) == {FABRIC_CONFIG_FILE}
    config = json.loads(launch.files[FABRIC_CONFIG_FILE])
    assert config["harness"] == {
        "adapter_id": FABRIC_ADAPTER,
        "resolution": "preinstalled",
        "settings": {
            "sandbox": "danger-full-access",
            "approval_mode": "deny_all",
            "config_overrides": {
                "analytics.enabled": False,
                "check_for_update_on_startup": False,
                "web_search": "disabled",
                "features.multi_agent": False,
                "model_providers.app.request_max_retries": 0,
                "model_providers.app.stream_max_retries": 0,
                "model_providers.app.supports_websockets": False,
            },
        },
    }
    assert config["models"] == {
        "default": {
            "provider": "app",
            "model": "target-model",
            "base_url": _GATEWAY.url,
            "api_key_env": "MODEL_API_KEY",
        }
    }
    assert "telemetry" not in config
    assert "relay" not in config
    assert all(_GATEWAY.api_key not in content for content in launch.files.values())
    assert _GATEWAY.url not in launch.run_command
    assert _GATEWAY.api_key not in launch.run_command
    assert 'MODEL_API_KEY="$APP_API_KEY" OPENAI_API_KEY= fabric -I -c ' in launch.run_command
    assert launch.run_command.endswith(f" {FABRIC_CONFIG_FILE}")
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key
    assert launch.env["APP_GATEWAY_URL"] == _GATEWAY.url
    assert "OPENAI_API_KEY" not in launch.env
    assert launch.instructions_file == "AGENTS.md"
    assert launch.parse_output is parse_plain_output


@pytest.mark.parametrize(
    ("effort", "setting"),
    [
        (None, None),
        ("none", "none"),
        ("minimal", "minimal"),
        ("high", "high"),
        ("xhigh", "xhigh"),
        ("max", "xhigh"),
        ("turbo", None),
    ],
)
def test_reasoning_effort_maps_to_the_codex_setting(effort: str | None, setting: str | None) -> None:
    """Pass the user's effort as Codex's ``reasoning_effort``, or no setting for Codex's default."""
    config = json.loads(build_launch(_target(), _GATEWAY, reasoning_effort=effort).files[FABRIC_CONFIG_FILE])
    assert config["harness"]["settings"].get("reasoning_effort") == setting


def test_protected_launch_checks_the_pin_offline_and_blocks_fallbacks_and_telemetry() -> None:
    """Use the offline version probe, blank the inherited OpenAI key, and turn telemetry off."""
    launch = build_launch(_target(), _GATEWAY, protected=True)

    assert launch.install_command == pinned_harness_check(BLACKBOX_HARNESS_FABRIC)
    assert launch.env["OPENAI_API_KEY"] == ""
    assert launch.env["OTEL_SDK_DISABLED"] == "true"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert launch.env["APP_API_KEY"] == _GATEWAY.api_key


def test_install_builds_the_pinned_venv_and_a_launcher_that_answers_the_version_probe(tmp_path: Path) -> None:
    """Install through fake uv: create the venv, request the exact pins, and skip it all on a second run."""
    install = build_launch(_target(), _GATEWAY).install_command or ""
    env, uv_log = _install(tmp_path, install)

    calls = uv_log.read_text().splitlines()
    assert calls[0].startswith("venv --python 3.12 ")
    assert calls[1].endswith("nemo-fabric-runtime==0.5.0 nemo-fabric-adapters-codex[harness]==0.5.0")
    version = subprocess.run(
        'export PATH="$HOME/.local/bin:$PATH"; fabric --version',
        shell=True,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert version.stdout.strip() == "fabric 0.5.0"
    subprocess.run(install, shell=True, cwd=tmp_path, env=env, capture_output=True, timeout=60, check=True)
    assert uv_log.read_text().splitlines() == calls


def _uv_download_install(
    tmp_path: Path, served: bytes, *, pinned: str | None = None
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run only the install's uv step, with ``curl`` and ``uname`` faked, the real ``sha256sum``, and no uv on PATH.

    Args:
        tmp_path: Scratch directory for the home, the fakes and the served tarball.
        served: The bytes the fake ``curl`` writes as the uv tarball.
        pinned: A digest to put in place of the real x86_64 pin, or ``None`` to keep it.

    Returns:
        The finished install and the ``$HOME`` it used.
    """
    install = build_launch(_target(), _GATEWAY).install_command or ""
    step = install[install.index('case "$(uname -m)"') : install.index('[ "$r" -eq 0 ] || exit "$r"')]
    if pinned is not None:
        step = step.replace(FABRIC_UV_SHA256["x86_64"], pinned)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    served_file = tmp_path / "served.tar.gz"
    served_file.write_bytes(served)
    for name, body in (
        ("uname", "#!/bin/sh\necho x86_64\n"),
        ("curl", f'#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\ncp {served_file} "$2"\n'),
    ):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    sha256sum = shutil.which("sha256sum")
    if sha256sum is None:
        pytest.skip("The uv step needs sha256sum.")
    home = tmp_path / "home"
    finished = subprocess.run(
        f'{step}[ "$r" -eq 0 ] || exit "$r"',
        shell=True,
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{Path(sha256sum).parent}{os.pathsep}{os.defpath}",
            "HOME": str(home),
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return finished, home


def test_uv_install_refuses_a_tarball_whose_digest_does_not_match(tmp_path: Path) -> None:
    """Fail the uv step on a swapped tarball, install no uv, and remove the download folder."""
    finished, home = _uv_download_install(tmp_path, b"not the uv release")

    assert finished.returncode == 1, finished.stderr
    assert not (home / ".local" / "bin" / "uv").exists()
    assert list(tmp_path.glob("tmp.*")) == []


def test_uv_install_unpacks_uv_when_the_digest_matches(tmp_path: Path) -> None:
    """Install ``uv`` from a tarball with the release layout once its digest matches the pin."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        body = b"#!/bin/sh\necho uv 0.13.7\n"
        member = tarfile.TarInfo("uv-x86_64-unknown-linux-musl/uv")
        member.size, member.mode = len(body), 0o755
        archive.addfile(member, io.BytesIO(body))
    served = buffer.getvalue()
    finished, home = _uv_download_install(tmp_path, served, pinned=hashlib.sha256(served).hexdigest())

    assert finished.returncode == 0, finished.stderr
    uv = home / ".local" / "bin" / "uv"
    assert subprocess.run([str(uv)], capture_output=True, text=True, check=True).stdout == "uv 0.13.7\n"
    assert list(tmp_path.glob("tmp.*")) == []


def test_uv_install_pins_one_digest_per_architecture() -> None:
    """Carry a sha256 digest for each musl tarball the install can pick."""
    install = build_launch(_target(), _GATEWAY).install_command or ""
    assert set(FABRIC_UV_SHA256) == {"x86_64", "aarch64"}
    assert all(len(digest) == 64 and f"s={digest};;" in install for digest in FABRIC_UV_SHA256.values())


def test_protected_run_points_codex_at_the_relay_and_returns_the_response(tmp_path: Path) -> None:
    """Run the protected command: the relay URL and gateway token reach the runner, and only the answer is on stdout."""
    finished, seen, workspace = _run(tmp_path, protected=True, result=_RECORDED_RESULT)

    assert finished.returncode == 0, finished.stderr
    assert seen["isolated"] == 1
    assert seen["env"] == {"MODEL_API_KEY": "gateway-token", "OPENAI_API_KEY": "", "APP_API_KEY": "gateway-token"}
    spec = seen["spec"]
    assert spec["config"]["models"]["default"]["base_url"] == _RELAY
    assert _GATEWAY.url not in json.dumps(spec)
    assert spec["request"] == {"input": "Fix `solution.py` and say $(echo done)."}
    assert spec["config"]["environment"]["workspace"] == str(workspace.resolve())
    state = Path(spec["config_base_dir"])
    assert not state.is_relative_to(workspace.resolve())
    assert spec["config"]["environment"]["artifacts"] == str(state / "artifacts")
    assert spec["logs_dir"] == str(state / "logs")
    assert "runner progress line" in finished.stderr
    assert "runner progress line" not in finished.stdout
    assert parse_output("plain", finished.stdout) == (_ANSWER, {})


def test_open_run_reads_the_gateway_url_from_the_config(tmp_path: Path) -> None:
    """Without a relay, the runner gets the gateway URL the launch wrote."""
    finished, seen, _ = _run(tmp_path, protected=False, result=_RECORDED_RESULT)

    assert finished.returncode == 0, finished.stderr
    assert seen["spec"]["config"]["models"]["default"]["base_url"] == _GATEWAY.url
    assert seen["env"]["OPENAI_API_KEY"] == ""


def test_runner_error_fails_the_run_with_its_message(tmp_path: Path) -> None:
    """Exit non-zero with the runner's message on stderr and nothing on stdout."""
    finished, _, _ = _run(tmp_path, protected=False, result=_RECORDED_FAILURE)

    assert finished.returncode == 1
    assert finished.stdout == ""
    assert "fabric: codex exited 2" in finished.stderr


def test_string_error_fails_the_run_with_that_string(tmp_path: Path) -> None:
    """Print a plain-string ``error`` as the failure message instead of only the status."""
    result = {"status": "failed", "error": "model route refused"}
    finished, _, _ = _run(tmp_path, protected=False, result=result)

    assert finished.returncode == 1
    assert finished.stdout == ""
    assert "fabric: model route refused" in finished.stderr


@pytest.mark.parametrize("result", [_RECORDED_RESULT, _RECORDED_FAILURE], ids=["succeeded", "runner_error"])
def test_run_removes_its_state_directory(tmp_path: Path, result: dict) -> None:
    """Leave no ``app-fabric-*`` folder behind, so repeated proposer turns do not fill the box."""
    finished, seen, _ = _run(tmp_path, protected=False, result=result)

    assert seen["spec"]["config_base_dir"].startswith(str(tmp_path / "tmp"))
    assert finished.returncode == (0 if result is _RECORDED_RESULT else 1), finished.stderr
    assert list((tmp_path / "tmp").glob("app-fabric-*")) == []


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="Root ignores the read-only mode, and non-POSIX systems handle chmod differently.",
)
def test_failed_relay_rewrite_stops_the_run_before_fabric_starts(tmp_path: Path) -> None:
    """Stop the protected command when the relay rewrite fails, so Fabric never runs with the parent gateway URL."""
    env, _ = _install(tmp_path, build_launch(_target(), _GATEWAY).install_command or "")
    launch = build_launch(_target(), _GATEWAY, protected=True)
    workspace = tmp_path / "work"
    config = workspace / FABRIC_CONFIG_FILE
    config.parent.mkdir(parents=True)
    config.write_text(launch.files[FABRIC_CONFIG_FILE])
    # A read-only config makes the rewrite fail while the file stays readable to Fabric.
    config.chmod(0o444)
    (workspace / "task").mkdir()
    (workspace / "task" / "PROMPT.md").write_text("Fix `solution.py`.")
    runner_log = tmp_path / "runner.json"
    env = {
        **env,
        **launch.env,
        # Fabric already on PATH, as in an image that ships it, so only the command's
        # own chaining can keep it from running after the failed rewrite.
        "PATH": f"{env['HOME']}/.local/bin{os.pathsep}{env['PATH']}",
        "APP_BUDGET_RELAY_URL": _RELAY,
        "FAKE_RUNNER_LOG": str(runner_log),
        "FAKE_RESULT": json.dumps(_RECORDED_RESULT),
    }
    finished = subprocess.run(
        launch.run_command, shell=True, cwd=workspace, env=env, capture_output=True, text=True, timeout=60, check=False
    )

    assert finished.returncode != 0
    assert finished.stdout == ""
    assert not runner_log.exists()


def test_recorded_output_parses_to_the_answer_and_the_payload_names_the_plain_parser() -> None:
    """Parse a recorded run-script stdout to its stripped text, and serialize the launch with the plain parser."""
    assert parse_plain_output(_RECORDED_RESULT["output"]["response"]) == (_ANSWER, {})
    assert parse_plain_output("  \n") == (None, {})
    assert launch_payload(build_launch(_target(), _GATEWAY))["output_format"] == "plain"
