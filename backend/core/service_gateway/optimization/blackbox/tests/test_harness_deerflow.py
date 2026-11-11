"""Tests for the DeerFlow harness: gateway-only launch, pins, protected mode and output parsing."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from .. import repo_tree
from ..agent_eval import candidate_files
from ..harness import GatewayConfig, build_launch, pinned_harness_check
from ..harness_bridge import parse_pi_output, workspace_files
from ..harnesses import REGISTRY
from ..harnesses.base import _version_check
from ..harnesses.deerflow import (
    CONFIG_FILE,
    DEERFLOW_PACKAGE,
    DEERFLOW_REF,
    DEERFLOW_REPO,
    HARNESS_ID,
    INSTRUCTIONS_FILE,
    MODEL_NAME,
    RUNNER_FILE,
    SPEC,
)

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_RELAY = "http://127.0.0.1:3210/v1"
_PROVIDER_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY")
_SEARCH_KEYS = ("TAVILY_API_KEY", "SERPER_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY", "JINA_API_KEY")
_TRACING_KEYS = ("LANGSMITH_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
# Synthetic stdout of one runner turn: DeerFlow log noise around the single
# Pi-shaped event the runner prints. No real model, key or account produced it.
_RECORDED_STDOUT = "\n".join(
    [
        "INFO deerflow.config: loaded config.yaml",
        "not json at all",
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Fixed the off-by-one in pager.py; tests pass."}],
                    "usage": {"input": 2210, "output": 388},
                },
            }
        ),
        "",
    ]
)

_FAKE_DEERFLOW = {
    "deerflow/__init__.py": "",
    "deerflow/client.py": """
import json
import os
from pathlib import Path
from types import SimpleNamespace

from langgraph.errors import GraphRecursionError


class DeerFlowClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def stream(self, message, recursion_limit):
        config = Path(os.environ["DEER_FLOW_PROJECT_ROOT"], "config.yaml").read_text()
        record = {
            "kwargs": self.kwargs,
            "message": message,
            "recursion_limit": recursion_limit,
            "config": json.loads(config),
            "keys": {name: os.environ.get(name) for name in ("OPENAI_API_KEY", "EXA_API_KEY")},
            "roots": [os.environ["DEER_FLOW_PROJECT_ROOT"], os.environ.get("DEER_FLOW_HOME")],
        }
        Path(os.environ["FAKE_DEERFLOW_RECORD"]).write_text(json.dumps(record))
        if os.environ.get("FAKE_DEERFLOW_EVENTS"):
            for kind, data in json.loads(os.environ["FAKE_DEERFLOW_EVENTS"]):
                yield SimpleNamespace(type=kind, data=data)
            if os.environ.get("FAKE_DEERFLOW_RECURSION"):
                raise GraphRecursionError("Recursion limit of 1000 reached")
            return
        yield SimpleNamespace(type="values", data={"title": "x"})
        yield SimpleNamespace(
            type="messages-tuple",
            data={"type": "ai", "id": "m1", "content": "draft", "usage_metadata": {"input_tokens": 3}},
        )
        yield SimpleNamespace(
            type="messages-tuple",
            data={"type": "ai", "id": "m2", "content": [{"type": "text", "text": "final answer"}]},
        )
        yield SimpleNamespace(type="messages-tuple", data={"type": "tool", "id": "t1", "content": "tool noise"})
        yield SimpleNamespace(type="end", data={"usage": {"input_tokens": 40, "output_tokens": 9}})
""",
    "langgraph/__init__.py": "",
    "langgraph/errors.py": "class GraphRecursionError(Exception):\n    pass\n",
}


def _target(**extra: str) -> BlackboxTarget:
    """Return an agent target on the DeerFlow harness.

    Args:
        **extra: Target field overrides.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, harness=HARNESS_ID, model="openrouter/acme/agent-1", **extra)


def test_registry_lists_deerflow_with_its_pin() -> None:
    """Register DeerFlow after goose, before cline, pinned to its exact source commit."""
    assert REGISTRY[HARNESS_ID] is SPEC
    assert HARNESS_ID in BLACKBOX_HARNESSES
    assert SPEC.output_format == "pi"
    assert SPEC.version == DEERFLOW_REF
    assert len(DEERFLOW_REF) == 40
    assert pinned_harness_check(HARNESS_ID) == _version_check(SPEC.binary, DEERFLOW_REF)


def test_launch_runs_the_runner_on_the_gateway_only() -> None:
    """Send every model call to the gateway and blank every direct, search and tracing key."""
    launch = build_launch(_target(), _GATEWAY)
    assert launch.parse_output is parse_pi_output
    assert launch.run_command == (
        'export DEERFLOW_DATA_DIR="$(mktemp -d)" && trap \'rm -rf "$DEERFLOW_DATA_DIR"\' EXIT && '
        'DEER_FLOW_PROJECT_ROOT="$DEERFLOW_DATA_DIR/project" '
        f'DEER_FLOW_HOME="$DEERFLOW_DATA_DIR/home" "$HOME/.app/deerflow/venv/bin/python" {RUNNER_FILE}'
    )
    assert set(launch.files) == {CONFIG_FILE, RUNNER_FILE}
    config = json.loads(launch.files[CONFIG_FILE])
    assert config["models"] == [
        {
            "name": MODEL_NAME,
            "use": "langchain_openai:ChatOpenAI",
            "model": "acme/agent-1",
            "base_url": "https://gw.example/v1",
            "api_key": "$APP_API_KEY",
            "request_timeout": 600.0,
            "max_retries": 2,
        }
    ]
    assert [group["name"] for group in config["tool_groups"]] == ["file:read", "file:write", "bash"]
    assert all(tool["use"].startswith("deerflow.sandbox.tools:") for tool in config["tools"])
    assert config["sandbox"]["use"] == "deerflow.sandbox.local:LocalSandboxProvider"
    assert "secret-key" not in launch.files[CONFIG_FILE] + launch.files[RUNNER_FILE]
    compile(launch.files[RUNNER_FILE], RUNNER_FILE, "exec")
    env = launch.env
    assert env["APP_API_KEY"] == "secret-key"
    assert env["APP_MODEL"] == "acme/agent-1"
    assert all(env[name] == "" for name in (*_PROVIDER_KEYS, *_SEARCH_KEYS, *_TRACING_KEYS))
    assert env["LANGSMITH_TRACING"] == "false"
    assert env["LANGCHAIN_TRACING_V2"] == "false"
    assert env["DO_NOT_TRACK"] == "1"
    assert not any(name.startswith("DEERFLOW_") for name in env)
    assert launch.instructions_file == INSTRUCTIONS_FILE == "AGENTS.md"


def test_install_clones_the_pinned_commit_into_a_venv() -> None:
    """Install DeerFlow from its exact commit and record that commit for the version probe."""
    install = build_launch(_target(), _GATEWAY).install_command
    assert install.startswith(f'export PATH="$HOME/.local/bin:$PATH"; {_version_check(SPEC.binary, DEERFLOW_REF)} || ')
    assert f"fetch --quiet --depth 1 {DEERFLOW_REPO} {DEERFLOW_REF}" in install
    assert "backend/packages/harness" in install
    assert "uv==0.9.13" in install
    assert f'printf "%s\\n" {DEERFLOW_REF} > "$HOME/.app/deerflow/venv/deerflow-ref"' in install
    assert "astral.sh" not in install


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        (None, {}),
        ("none", {}),
        ("NONE", {}),
        ("low", {"supports_reasoning_effort": True, "reasoning_effort": "low"}),
        ("high", {"supports_reasoning_effort": True, "reasoning_effort": "high"}),
    ],
)
def test_effort_reaches_the_model_entry(effort: str | None, expected: dict[str, object]) -> None:
    """Put the user's effort on the model entry, so DeerFlow's factory sends it to the gateway.

    Args:
        effort: Reasoning effort the user picked, or ``None``.
        expected: The effort fields the model entry must carry.
    """
    entry = json.loads(build_launch(_target(), _GATEWAY, reasoning_effort=effort).files[CONFIG_FILE])["models"][0]
    assert {key: entry[key] for key in ("supports_reasoning_effort", "reasoning_effort") if key in entry} == expected
    assert entry["base_url"] == _GATEWAY.url


def test_protected_launch_uses_the_pin_relay_and_offline_env(tmp_path: Path) -> None:
    """Probe the preinstalled pin, point the config at the relay, and stay offline.

    Args:
        tmp_path: Workspace the protected run command rewrites.
    """
    launch = build_launch(_target(), _GATEWAY, protected=True)
    assert launch.install_command == pinned_harness_check(HARNESS_ID)
    env = launch.env
    assert env["UV_OFFLINE"] == "1"
    assert env["DO_NOT_TRACK"] == "1"
    assert all(env[name] == "" for name in (*_PROVIDER_KEYS, *_SEARCH_KEYS, *_TRACING_KEYS))
    for name, source in launch.files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(source)
    # The runner is missing from the fake home, so the command fails after the rewrite.
    subprocess.run(
        ["bash", "-c", launch.run_command],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "APP_BUDGET_RELAY_URL": _RELAY},
        capture_output=True,
        timeout=60,
        check=False,
    )
    config = json.loads((tmp_path / CONFIG_FILE).read_text())
    assert config["models"][0]["base_url"] == _RELAY
    assert all(_GATEWAY.url not in (tmp_path / name).read_text() for name in launch.files)
    assert repr(_RELAY) in (tmp_path / RUNNER_FILE).read_text()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "gw.example/v1",
        "ftp://gw.example/v1",
        "https://user:pw@gw.example/v1",
        "https://gw.example/v1'x",
        "https://gw.exämple/v1",
        "https://",
    ],
)
def test_bad_gateway_url_refuses_the_launch(url: str) -> None:
    """Refuse a gateway URL that would let DeerFlow fall back to another route.

    Args:
        url: Malformed gateway URL.
    """
    with pytest.raises(ServiceError):
        build_launch(_target(), GatewayConfig(url=url, api_key="secret-key"))


def test_parser_reads_the_recorded_runner_output() -> None:
    """Turn the recorded runner stdout into the answer and token counts."""
    assert parse_pi_output(_RECORDED_STDOUT) == (
        "Fixed the off-by-one in pager.py; tests pass.",
        {"input_tokens": 2210, "output_tokens": 388},
    )


def _run_fake_runner(
    tmp_path: Path, instructions: str | None = None, **extra_env: str
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    """Run the shipped runner against a fake DeerFlow package.

    Args:
        tmp_path: Workspace and fake package root.
        instructions: Text for the workspace's instructions file, or ``None`` for no file.
        **extra_env: Extra environment for the runner, such as scripted events.

    Returns:
        The finished process, the workspace and the fake client's record file.
    """
    launch = build_launch(_target(), _GATEWAY, reasoning_effort="none")
    fakes = tmp_path / "fakes"
    for name, source in _FAKE_DEERFLOW.items():
        (fakes / name).parent.mkdir(parents=True, exist_ok=True)
        (fakes / name).write_text(source)
    workspace = tmp_path / "work"
    for name, source in launch.files.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(source)
    (workspace / "task").mkdir()
    (workspace / "task" / "PROMPT.md").write_text("Fix the pager.\n")
    if instructions is not None:
        (workspace / INSTRUCTIONS_FILE).write_text(instructions)
    record = tmp_path / "record.json"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(fakes),
        **launch.env,
        "APP_PROMPT_FILE": "task/PROMPT.md",
        "OPENAI_API_KEY": "leaked-host-key",
        "DEER_FLOW_PROJECT_ROOT": str(workspace / ".app/deerflow/project"),
        "FAKE_DEERFLOW_RECORD": str(record),
        **extra_env,
    }
    completed = subprocess.run(
        [sys.executable, RUNNER_FILE], cwd=workspace, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    return completed, workspace, record


def test_runner_drives_the_client_and_prints_one_pi_event(tmp_path: Path) -> None:
    """Run the shipped runner against a fake DeerFlow and parse what it prints.

    Args:
        tmp_path: Workspace and fake package root.
    """
    completed, workspace, record = _run_fake_runner(tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert parse_pi_output(completed.stdout) == ("final answer", {"input_tokens": 40, "output_tokens": 9})
    seen = json.loads(record.read_text())
    assert seen["kwargs"] == {
        "model_name": MODEL_NAME,
        "subagent_enabled": False,
        "thinking_enabled": False,
        "plan_mode": False,
    }
    assert seen["recursion_limit"] == 1000
    assert seen["message"].endswith("Task:\nFix the pager.")
    assert INSTRUCTIONS_FILE not in seen["message"]
    assert str(workspace.resolve()) in seen["message"]
    assert seen["keys"] == {"OPENAI_API_KEY": None, "EXA_API_KEY": None}
    assert seen["config"]["models"][0]["base_url"] == _GATEWAY.url
    mount = {"host_path": str(workspace.resolve()), "container_path": str(workspace.resolve()), "read_only": False}
    assert seen["config"]["sandbox"]["mounts"] == [mount]


@pytest.mark.parametrize("candidate", ["Always run the tests first.\n", "Use {braces} and 100% care."])
def test_runner_puts_the_candidate_instructions_in_the_turn_message(tmp_path: Path, candidate: str) -> None:
    """Send the text version, written to the instructions file, to DeerFlow before the task.

    DeerFlow loads no AGENTS.md itself, so without this the candidate never reaches the model.

    Args:
        tmp_path: Workspace and fake package root.
        candidate: The text version the optimizer writes to the instructions file.
    """
    files = candidate_files(candidate, build_launch(_target(), _GATEWAY).instructions_file)
    completed, _, record = _run_fake_runner(tmp_path, instructions=files[INSTRUCTIONS_FILE])
    assert completed.returncode == 0, completed.stderr
    message = json.loads(record.read_text())["message"]
    section = f"Follow these instructions from {INSTRUCTIONS_FILE}:\n{candidate.strip()}\n\nTask:\nFix the pager."
    assert message.endswith(section)


def test_runner_skips_an_empty_instructions_file(tmp_path: Path) -> None:
    """Send only the task when the instructions file holds no text.

    Args:
        tmp_path: Workspace and fake package root.
    """
    completed, _, record = _run_fake_runner(tmp_path, instructions="  \n")
    assert completed.returncode == 0, completed.stderr
    message = json.loads(record.read_text())["message"]
    assert INSTRUCTIONS_FILE not in message
    assert message.endswith("/mnt/user-data.\n\nTask:\nFix the pager.")


def _ai(message_id: str, content: object) -> list[object]:
    """Return one scripted ``messages-tuple`` event for an AI message piece.

    Args:
        message_id: Stable id that DeerFlow gives every piece of one message.
        content: The piece's LangChain content value.

    Returns:
        The event as a ``[type, data]`` pair.
    """
    return ["messages-tuple", {"type": "ai", "id": message_id, "content": content}]


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([_ai("m1", "The answer "), _ai("m1", "is "), _ai("m1", "42.")], "The answer is 42."),
        (
            [
                _ai("m1", "Let me "),
                _ai("m1", "look."),
                ["messages-tuple", {"type": "tool", "id": "t1", "content": "tool noise"}],
                _ai("m2", [{"type": "text", "text": "Done: "}]),
                _ai("m2", ""),
                _ai("m2", "fixed."),
            ],
            "Done: fixed.",
        ),
    ],
)
def test_runner_joins_streamed_deltas_of_the_last_ai_message(
    tmp_path: Path, events: list[list[object]], expected: str
) -> None:
    """Join the text deltas of each AI message id and answer with the last message.

    Args:
        tmp_path: Workspace and fake package root.
        events: Scripted DeerFlow stream events.
        expected: The answer the runner must print.
    """
    completed, _, _ = _run_fake_runner(tmp_path, FAKE_DEERFLOW_EVENTS=json.dumps(events))
    assert completed.returncode == 0, completed.stderr
    assert parse_pi_output(completed.stdout)[0] == expected


def test_runner_joins_deltas_that_carry_no_message_id(tmp_path: Path) -> None:
    """Join id-less AI deltas into one answer instead of keeping only the last one.

    Args:
        tmp_path: Workspace and fake package root.
    """
    events = [
        ["messages-tuple", {"type": "ai", "content": "The answer "}],
        ["messages-tuple", {"type": "ai", "id": None, "content": "is 42."}],
    ]
    completed, _, _ = _run_fake_runner(tmp_path, FAKE_DEERFLOW_EVENTS=json.dumps(events))
    assert completed.returncode == 0, completed.stderr
    assert parse_pi_output(completed.stdout)[0] == "The answer is 42."


def test_runner_sums_usage_once_per_message_without_an_end_event(tmp_path: Path) -> None:
    """Without a final usage event, count each AI message's usage once, however many chunks repeat it.

    Args:
        tmp_path: Workspace and fake package root.
    """
    m1 = {"input_tokens": 10, "output_tokens": 2}
    no_id = {"input_tokens": 7, "output_tokens": 3}
    events = [
        ["messages-tuple", {"type": "ai", "id": "m1", "content": "a", "usage_metadata": m1}],
        ["messages-tuple", {"type": "ai", "id": "m1", "content": "b", "usage_metadata": m1}],
        ["messages-tuple", {"type": "ai", "id": "m2", "content": "c", "usage_metadata": {"input_tokens": 5}}],
        ["messages-tuple", {"type": "ai", "content": "d", "usage_metadata": no_id}],
        ["messages-tuple", {"type": "ai", "id": None, "content": "e", "usage_metadata": no_id}],
    ]
    completed, _, _ = _run_fake_runner(tmp_path, FAKE_DEERFLOW_EVENTS=json.dumps(events))
    assert completed.returncode == 0, completed.stderr
    assert parse_pi_output(completed.stdout)[1] == {"input_tokens": 22, "output_tokens": 5}


def test_runner_fails_on_deerflow_provider_error_fallback(tmp_path: Path) -> None:
    """Exit non-zero with no ``message_end`` when DeerFlow replaces the reply with its error fallback.

    Args:
        tmp_path: Workspace and fake package root.
    """
    fallback = {
        "type": "ai",
        "id": "m1",
        "content": "The model provider is unavailable.",
        "additional_kwargs": {"deerflow_error_fallback": True, "error_detail": "upstream 503"},
    }
    events = [["messages-tuple", fallback], ["end", {"usage": {"input_tokens": 4, "output_tokens": 0}}]]
    completed, _, _ = _run_fake_runner(tmp_path, FAKE_DEERFLOW_EVENTS=json.dumps(events))
    assert completed.returncode != 0
    assert "message_end" not in completed.stdout
    assert "upstream 503" in completed.stderr


def test_runner_fails_when_deerflow_hits_the_recursion_limit(tmp_path: Path) -> None:
    """Exit non-zero at the recursion limit, reporting partial usage and no answer.

    The usage shows the model did work, so a run that dies here is not taken
    for a harness that never reached the model.

    Args:
        tmp_path: Workspace and fake package root.
    """
    m1 = {"input_tokens": 10, "output_tokens": 2}
    events = [
        ["messages-tuple", {"type": "ai", "id": "m1", "content": "partial", "usage_metadata": m1}],
        ["messages-tuple", {"type": "ai", "id": "m1", "content": " text", "usage_metadata": m1}],
        ["messages-tuple", {"type": "ai", "id": "m2", "content": "", "usage_metadata": {"input_tokens": 5}}],
    ]
    completed, _, _ = _run_fake_runner(tmp_path, FAKE_DEERFLOW_EVENTS=json.dumps(events), FAKE_DEERFLOW_RECURSION="1")
    assert completed.returncode == 1
    assert parse_pi_output(completed.stdout) == (None, {"input_tokens": 15, "output_tokens": 2})
    ends = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    assert [end["message"].get("partial") for end in ends] == [True]
    assert "recursion limit reached" in completed.stderr


@pytest.mark.parametrize("recursion", [False, True])
def test_run_command_keeps_deerflow_state_out_of_the_version_patch(tmp_path: Path, recursion: bool) -> None:
    """Write DeerFlow's state outside the checkout, remove it at exit, and keep the exit status.

    Args:
        tmp_path: Shipped tree, checkout and fake package root.
        recursion: Whether the fake DeerFlow stops at the recursion limit.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('hi')\n")
    archive = tmp_path / "tree.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    chunks = []
    for index, chunk in enumerate(repo_tree.archive_chunks(archive)):
        chunks.append(tmp_path / f"tree.{index}.b64")
        chunks[-1].write_text(chunk)
    checkout = repo_tree.unpack_tree(chunks, tmp_path / "repo-checkout")
    launch = build_launch(_target(), _GATEWAY)
    repo_tree.exclude_paths(checkout, workspace_files({"files": launch.files}))
    for name, text in launch.files.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    fakes = tmp_path / "fakes"
    for name, text in _FAKE_DEERFLOW.items():
        (fakes / name).parent.mkdir(parents=True, exist_ok=True)
        (fakes / name).write_text(text)
    prompt = tmp_path / "PROMPT.md"
    prompt.write_text("Fix it.\n")
    record = tmp_path / "record.json"
    completed = subprocess.run(
        [
            "bash",
            "-c",
            launch.run_command.replace('"$HOME/.app/deerflow/venv/bin/python"', shlex.quote(sys.executable)),
        ],
        cwd=checkout,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(fakes),
            **launch.env,
            "APP_PROMPT_FILE": str(prompt),
            "FAKE_DEERFLOW_RECORD": str(record),
            **(
                {"FAKE_DEERFLOW_EVENTS": json.dumps([_ai("m1", "x")]), "FAKE_DEERFLOW_RECURSION": "1"}
                if recursion
                else {}
            ),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == (1 if recursion else 0), completed.stderr
    seen = json.loads(record.read_text())
    roots = [Path(root) for root in seen["roots"]]
    data_dir = roots[0].parent
    try:
        assert seen["config"]["models"][0]["base_url"] == _GATEWAY.url
        assert roots == [data_dir / "project", data_dir / "home"]
        assert not data_dir.exists()
        assert not data_dir.resolve().is_relative_to(checkout.resolve())
        assert repo_tree.version_patch(checkout) == ""
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_runner_refuses_a_config_with_another_model_route(tmp_path: Path) -> None:
    """Stop before DeerFlow loads when the config routes the model off the gateway.

    Args:
        tmp_path: Workspace with a tampered config.
    """
    launch = build_launch(_target(), _GATEWAY)
    for name, source in launch.files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(source.replace(_GATEWAY.url, "https://api.openai.com/v1"))
    (tmp_path / RUNNER_FILE).write_text(launch.files[RUNNER_FILE])
    completed = subprocess.run(
        [sys.executable, RUNNER_FILE],
        cwd=tmp_path,
        env={**launch.env, "PATH": os.environ.get("PATH", ""), "DEER_FLOW_PROJECT_ROOT": str(tmp_path / "project")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 1
    assert "through the gateway" in completed.stderr
    assert not (tmp_path / "project").exists()


def _fake_venv(home: Path, marker: str) -> None:
    """Create the DeerFlow venv layout with a fake installed package and a ref marker.

    Args:
        home: Fake ``$HOME``.
        marker: Commit the install recorded.
    """
    root = home / ".app/deerflow/venv"
    venv.create(root, with_pip=False, symlinks=True)
    site = next(root.glob("lib/python*/site-packages"))
    dist = site / "deerflow_harness-0.1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {DEERFLOW_PACKAGE}\nVersion: 0.1.0\n")
    (root / "deerflow-ref").write_text(marker + "\n")


@pytest.mark.parametrize(("marker", "code"), [(DEERFLOW_REF, 0), ("0" * 40, 1)])
def test_version_probe_accepts_only_the_pinned_commit(tmp_path: Path, marker: str, code: int) -> None:
    """Pass the pin probe only when the venv records the exact pinned commit.

    Args:
        tmp_path: Fake home directory.
        marker: Commit the fake install recorded.
        code: Expected probe exit status.
    """
    _fake_venv(tmp_path, marker)
    completed = subprocess.run(
        ["bash", "-c", pinned_harness_check(HARNESS_ID)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
        timeout=60,
        check=False,
    )
    assert completed.returncode == code
