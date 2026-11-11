"""Tests for the Terminus 2 harness: gateway-only launch, pins, effort, protected mode and output parsing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import litellm
import pytest

from core.api.model_catalog import REASONING_EFFORTS
from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_TARGET_AGENT, BlackboxTarget

from ..harness import GatewayConfig, build_launch, pinned_harness_check
from ..harness_bridge import parse_output, parse_pi_output
from ..harnesses import REGISTRY
from ..harnesses.base import _version_check
from ..harnesses.terminus import ENV_REASONING_EFFORT, HARBOR_VERSION, HARNESS_ID, RUNNER_FILE, SPEC
from .mocks import FakeGateway

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_RELAY = "http://127.0.0.1:3210/v1"
_DIRECT_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
)
_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# Synthetic stdout of one runner turn: uv noise around the single Pi-shaped
# event the runner prints. No real model, key or account produced it.
_RECORDED_STDOUT = "\n".join(
    [
        "Installed 112 packages in 1.2s",
        "not json at all",
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "The file is written."}],
                    "usage": {"input": 200, "output": 40},
                },
            }
        ),
        "",
    ]
)

# A fake ``harbor`` package with the four names the runner imports. The fake
# agent runs one command in the shell the runner gives it and records what it saw.
_FAKE_HARBOR = {
    "harbor/__init__.py": "",
    "harbor/agents/__init__.py": "",
    "harbor/environments/__init__.py": "",
    "harbor/models/__init__.py": "",
    "harbor/models/agent/__init__.py": "",
    "harbor/environments/base.py": """
class ExecResult:
    def __init__(self, stdout, stderr, return_code):
        self.stdout, self.stderr, self.return_code = stdout, stderr, return_code
""",
    "harbor/models/agent/context.py": """
class AgentContext:
    n_input_tokens = None
    n_output_tokens = None
""",
    "harbor/agents/terminus_2/tmux_session.py": """
class TmuxSession:
    started = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def start(self):
        TmuxSession.started = self.kwargs
""",
    "harbor/agents/terminus_2/__init__.py": """
import json
import os
from types import SimpleNamespace

from harbor.agents.terminus_2.tmux_session import TmuxSession


class Terminus2:
    def __init__(self, logs_dir, model_name, api_base, llm_kwargs, reasoning_effort, record_terminal_session):
        self.seen = {
            "logs_dir": str(logs_dir),
            "model_name": model_name,
            "api_base": api_base,
            "llm_kwargs": llm_kwargs,
            "reasoning_effort": reasoning_effort,
            "record_terminal_session": record_terminal_session,
        }
        self.options = SimpleNamespace(tmux_pane_width=160, tmux_pane_height=40)
        self._extra_env = {}
        self._chat = None

    @staticmethod
    def name():
        return "terminus-2"

    async def run(self, instruction, environment, context):
        result = await environment.exec("echo hi > out.txt; env")
        shell_env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        tmux = TmuxSession.started
        record = {
            **self.seen,
            "instruction": instruction,
            "exit": result.return_code,
            "shell_keys": sorted(k for k in ("APP_API_KEY", "SKYNET_API_KEY") if k in shell_env),
            "tmux_tmpdir_set": "TMUX_TMPDIR" in shell_env,
            "pane_log_in_logs_parent": str(tmux["logging_path"]).startswith(str(os.path.dirname(self.seen["logs_dir"]))),
            "asciinema": [tmux["local_asciinema_recording_path"], tmux["remote_asciinema_recording_path"]],
        }
        with open(os.environ["FAKE_HARBOR_RECORD"], "w") as handle:
            json.dump(record, handle)
        context.n_input_tokens, context.n_output_tokens = 11, 7
        self._chat = SimpleNamespace(
            messages=[
                {"role": "user", "content": instruction},
                {"role": "assistant", "content": json.dumps({"analysis": "answer to " + instruction.strip()})},
            ]
        )
""",
}


def _fake_harbor(root: Path) -> Path:
    """Write the fake ``harbor`` package so the runner executes without the real one.

    Args:
        root: Directory to hold the fake package.

    Returns:
        The directory to put on ``PYTHONPATH``.
    """
    for name, content in _FAKE_HARBOR.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def _run_runner(
    tmp_path: Path, launch_files: dict[str, str], *, tmux: bool = True, **env: str
) -> subprocess.CompletedProcess:
    """Run the generated runner with the current Python against the fake Harbor package.

    Args:
        tmp_path: Test directory; the workspace and fake package live under it.
        launch_files: Generated launch files to write into the workspace.
        tmux: Whether a fake ``tmux`` is on ``PATH``.
        **env: Extra environment variables.

    Returns:
        The finished process.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if tmux:
        fake_tmux = bin_dir / "tmux"
        fake_tmux.write_text("#!/bin/sh\nexit 0\n")
        fake_tmux.chmod(0o755)
    workspace = tmp_path / "ws"
    for name, content in launch_files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return subprocess.run(
        [sys.executable, RUNNER_FILE],
        cwd=workspace,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.defpath}", "PYTHONPATH": str(_fake_harbor(tmp_path / "fake")), **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_spec_is_registered_with_a_pinned_version_and_the_pi_parser() -> None:
    """The registry lists ``terminus`` with the exact Harbor pin, an offline probe and an existing parser."""
    assert REGISTRY[HARNESS_ID] is SPEC
    assert HARNESS_ID == "terminus"
    assert SPEC.version == HARBOR_VERSION == "0.25.0"
    assert SPEC.output_format == "pi"
    assert parse_output("pi", _RECORDED_STDOUT) == parse_pi_output(_RECORDED_STDOUT)
    assert "--offline" in SPEC.binary
    assert "--with harbor==0.25.0" in SPEC.binary


def test_launch_routes_through_the_gateway_with_blank_provider_keys_and_no_telemetry() -> None:
    """The env carries the gateway under both names, blanks every direct key and turns telemetry off."""
    launch = SPEC.launch("gpt-x", _GATEWAY)

    assert launch.run_command == 'PATH="$HOME/.local/bin:$PATH" uv run --quiet --script .app/terminus/run_agent.py'
    assert launch.instructions_file == "AGENTS.md"
    for prefix in ("APP_", "SKYNET_"):
        assert launch.env[f"{prefix}API_KEY"] == _GATEWAY.api_key
        assert launch.env[f"{prefix}MODEL"] == "gpt-x"
    for name in _DIRECT_KEYS:
        assert launch.env[name] == ""
    assert launch.env["HARBOR_TELEMETRY"] == "0"
    assert launch.env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert launch.env["DO_NOT_TRACK"] == "1"
    assert ENV_REASONING_EFFORT not in launch.env
    assert launch.parse_output is parse_pi_output
    for content in launch.files.values():
        assert _GATEWAY.api_key not in content


def test_runner_pins_harbor_and_embeds_the_gateway_url() -> None:
    """The uv script header pins Harbor and its model client, and the URL is a literal in the script."""
    runner = SPEC.launch("m", _GATEWAY).files[RUNNER_FILE]

    for pin in ("harbor==0.25.0", "litellm==1.93.0", "openai==2.24.0"):
        assert f'#   "{pin}",' in runner
    assert '# requires-python = ">=3.12,<3.15"' in runner
    assert f"GATEWAY_URL = {_GATEWAY.url!r}" in runner
    compile(runner, RUNNER_FILE, "exec")


def test_install_checks_the_pin_then_installs_uv_and_warms_the_cache() -> None:
    """The install skips work when the pinned Harbor is warm and otherwise installs the exact uv pin."""
    install = SPEC.launch("m", _GATEWAY).install_command

    assert install is not None
    assert install.startswith(f'export PATH="$HOME/.local/bin:$PATH"; {_version_check(SPEC.binary, HARBOR_VERSION)} ||')
    assert "uv==0.9.13" in install
    assert "uv run --quiet --no-project" in install


@pytest.mark.parametrize("effort", _EFFORTS)
def test_every_supported_effort_passes_unchanged(effort: str) -> None:
    """Each Skynet effort reaches the runner as the same string, with no remap."""
    assert SPEC.launch("m", _GATEWAY, reasoning_effort=effort).env[ENV_REASONING_EFFORT] == effort


@pytest.mark.parametrize("effort", [None, ""])
def test_no_effort_is_left_to_the_model_default(effort: str | None) -> None:
    """Without an effort, the runner gets none and the model default applies."""
    assert ENV_REASONING_EFFORT not in SPEC.launch("m", _GATEWAY, reasoning_effort=effort).env


@pytest.mark.parametrize("effort", ["default", "HIGH", "turbo"])
def test_unsupported_effort_stops_the_launch(effort: str) -> None:
    """An effort Terminus 2 does not accept is refused, not dropped or remapped."""
    with pytest.raises(ServiceError, match="reasoning effort"):
        SPEC.launch("m", _GATEWAY, reasoning_effort=effort)


def test_spec_declares_every_skynet_effort_for_the_submit_check() -> None:
    """Submit offers and accepts exactly the levels the launch passes unchanged (SKY-585)."""
    assert SPEC.supported_efforts == REASONING_EFFORTS


@pytest.mark.parametrize(
    "url",
    ["", "gw.example/v1", "ftp://gw.example", "https://user:pw@gw.example", "https://gw.example/v1'", "https://gw ex"],
)
def test_launch_refuses_a_bad_gateway_url(url: str) -> None:
    """Without a clean absolute http(s) URL there is no launch, so no direct provider route."""
    with pytest.raises(ServiceError):
        SPEC.launch("m", GatewayConfig(url=url, api_key="secret-key"))


def test_protected_launch_checks_the_pin_offline_and_rewrites_the_runner_to_the_relay(tmp_path: Path) -> None:
    """Protected mode uses the version probe, offline uv env, and points the runner at the local relay."""
    target = BlackboxTarget.model_construct(
        kind=BLACKBOX_TARGET_AGENT, harness=HARNESS_ID, model="m", run_command="true"
    )
    launch = build_launch(target, _GATEWAY, protected=True)

    assert launch.install_command is not None
    assert launch.install_command == pinned_harness_check(HARNESS_ID)
    assert "pip install" not in launch.install_command
    assert launch.env["UV_OFFLINE"] == "1"
    assert launch.env["HARBOR_TELEMETRY"] == "0"
    for name in _DIRECT_KEYS:
        assert launch.env[name] == ""
    for name, content in launch.files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(
        launch.run_command,
        shell=True,
        cwd=tmp_path,
        env={"PATH": os.defpath, "APP_BUDGET_RELAY_URL": _RELAY},
        timeout=10,
        check=True,
    )
    runner = (tmp_path / RUNNER_FILE).read_text()
    assert _GATEWAY.url not in runner
    assert f"GATEWAY_URL = {_RELAY!r}" in runner


@pytest.mark.parametrize(("reported", "ok"), [("0.25.0", True), ("0.25.1", False), ("10.25.0", False)])
def test_pinned_check_accepts_only_the_exact_harbor_version(tmp_path: Path, reported: str, ok: bool) -> None:
    """The offline probe passes only when the warmed environment reports the pinned Harbor release."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(f"#!/bin/sh\necho {reported}\n")
    fake_uv.chmod(0o755)
    check = pinned_harness_check(HARNESS_ID)
    assert check is not None

    result = subprocess.run(
        check,
        shell=True,
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}{os.pathsep}{os.defpath}"},
        timeout=10,
        check=False,
    )

    assert (result.returncode == 0) is ok


def test_runner_drives_terminus_through_the_gateway_and_prints_a_pi_event(tmp_path: Path) -> None:
    """Against a fake Harbor, the runner wires the gateway, keeps the token out of the shell and cleans up."""
    launch = SPEC.launch("gpt-x", _GATEWAY, reasoning_effort="xhigh")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Write out.txt.\n")
    record = tmp_path / "record.json"
    env = {k: v for k, v in launch.env.items() if not k.startswith("APP_")}
    env.update(SKYNET_PROMPT_FILE=str(prompt), FAKE_HARBOR_RECORD=str(record), TMPDIR=str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()

    result = _run_runner(tmp_path, launch.files, **env)

    assert result.returncode == 0, result.stderr
    seen = json.loads(record.read_text())
    assert seen == {
        "logs_dir": seen["logs_dir"],
        "model_name": "openai/gpt-x",
        "api_base": _GATEWAY.url,
        "llm_kwargs": {"api_key": _GATEWAY.api_key, "extra_body": {"reasoning_effort": "xhigh"}},
        "reasoning_effort": None,
        "record_terminal_session": False,
        "instruction": "Write out.txt.\n",
        "exit": 0,
        "shell_keys": [],
        "tmux_tmpdir_set": True,
        "pane_log_in_logs_parent": True,
        "asciinema": [None, None],
    }
    assert seen["logs_dir"].startswith(str(tmp_path / "tmp"))
    assert list((tmp_path / "tmp").iterdir()) == []
    assert (tmp_path / "ws" / "out.txt").read_text() == "hi\n"
    assert parse_pi_output(result.stdout) == ("answer to Write out.txt.", {"input_tokens": 11, "output_tokens": 7})


def test_runner_without_an_effort_sends_no_effort(tmp_path: Path) -> None:
    """Without an effort, the runner adds no ``extra_body`` and the model default applies."""
    launch = SPEC.launch("gpt-x", _GATEWAY)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Fix it.\n")
    record = tmp_path / "record.json"
    env = {**launch.env, "APP_PROMPT_FILE": str(prompt), "FAKE_HARBOR_RECORD": str(record)}

    result = _run_runner(tmp_path, launch.files, **env)

    assert result.returncode == 0, result.stderr
    seen = json.loads(record.read_text())
    assert (seen["llm_kwargs"], seen["reasoning_effort"]) == ({"api_key": _GATEWAY.api_key}, None)


@pytest.mark.parametrize("model", ["openai/gpt-5.5", "openai/gpt-5.2", "openai/o3", "openai/gateway-model"])
@pytest.mark.parametrize("effort", _EFFORTS)
def test_litellm_sends_the_extra_body_effort_unchanged_with_drop_params(model: str, effort: str) -> None:
    """LiteLLM puts the ``extra_body`` effort in the request as is, even for a gpt-5 id with ``drop_params``.

    This uses the backend's LiteLLM, not the box's 1.93.0 pin. A check with the
    pinned Harbor and LiteLLM is in the PR body.

    Args:
        model: A LiteLLM model id on the ``openai/`` route.
        effort: A Skynet effort level.
    """
    with FakeGateway(reply="ok") as gateway:
        litellm.completion(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            api_base=gateway.url,
            api_key="k",
            drop_params=True,
            extra_body={"reasoning_effort": effort},
        )

    assert gateway.requests[-1]["body"]["reasoning_effort"] == effort


def test_runner_refuses_without_the_gateway_token(tmp_path: Path) -> None:
    """The runner exits before any agent call when the gateway token is missing."""
    launch = SPEC.launch("m", _GATEWAY)
    record = tmp_path / "record.json"
    env = {**launch.env, "APP_API_KEY": "", "SKYNET_API_KEY": "", "FAKE_HARBOR_RECORD": str(record)}

    result = _run_runner(tmp_path, launch.files, **env)

    assert result.returncode != 0
    assert "gateway" in result.stderr
    assert not record.exists()


def test_runner_stops_when_tmux_is_missing(tmp_path: Path) -> None:
    """Without tmux the runner exits with a clear message instead of Harbor's root package install."""
    launch = SPEC.launch("m", _GATEWAY)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Fix it.\n")
    record = tmp_path / "record.json"
    env = {**launch.env, "APP_PROMPT_FILE": str(prompt), "FAKE_HARBOR_RECORD": str(record)}

    result = _run_runner(tmp_path, launch.files, tmux=False, **env)

    assert result.returncode != 0
    assert "needs tmux" in result.stderr
    assert not record.exists()


def test_recorded_output_sample_parses_to_the_answer_and_usage() -> None:
    """A recorded runner stdout turns into the final answer and token usage."""
    assert parse_pi_output(_RECORDED_STDOUT) == ("The file is written.", {"input_tokens": 200, "output_tokens": 40})
