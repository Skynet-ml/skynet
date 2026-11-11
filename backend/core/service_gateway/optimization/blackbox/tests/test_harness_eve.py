"""Tests for the eve harness: registration, the generated project, gateway-only env and the runner output."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

from core.models.blackbox import BLACKBOX_HARNESS_EVE, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from .. import repo_tree
from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import _Monitor, parse_output, parse_pi_output, tool_call_ids, workspace_files
from ..harnesses import REGISTRY
from ..harnesses.eve import (
    AI_SDK_VERSION,
    EVE_CHANNEL,
    EVE_IMAGE_DEPS,
    EVE_INSTRUCTIONS_FILE,
    EVE_RUNNER,
    EVE_SANDBOX,
    EVE_VERSION,
    OPENAI_COMPATIBLE_VERSION,
    ZOD_VERSION,
)

_SANDBOX_RUNTIME = Path(__file__).resolve().parents[5] / "sandbox-runtime"

_GATEWAY = GatewayConfig(url="https://gw.example/v1", api_key="secret-key")
_PROVIDER_KEYS = (
    "AI_GATEWAY_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "VERCEL_OIDC_TOKEN",
)
# A synthetic runner transcript in the shape runner.mjs prints: one line per
# requested eve action, then the final answer with the summed step usage.
_RUNNER_SAMPLE = "\n".join(
    [
        "eve build complete",
        json.dumps({"type": "tool_execution_start", "toolCallId": "act_1"}),
        json.dumps({"type": "tool_execution_start", "toolCallId": "act_2"}),
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "The answer is 42."}],
                    "usage": {"input": 1200, "output": 85},
                },
            }
        ),
    ]
)


def _target(**overrides: str) -> BlackboxTarget:
    """Build an eve agent target.

    Args:
        overrides: Extra target fields.

    Returns:
        The target.
    """
    return BlackboxTarget(kind=BLACKBOX_TARGET_AGENT, model="openrouter/acme/agent-1", harness="eve", **overrides)


def test_eve_is_registered_after_prime_and_before_cline() -> None:
    """List eve in the registry and the model ids after prime and before cline, at a unique position."""
    spec = REGISTRY[BLACKBOX_HARNESS_EVE]
    assert (spec.binary, spec.version, spec.output_format, spec.position) == ("eve", "1.0.0-rc.6", "pi", 66)
    assert [other.id for other in REGISTRY.values() if other.position == spec.position] == ["eve"]
    for names in (list(BLACKBOX_HARNESSES), list(REGISTRY)):
        assert names.index("prime") < names.index("eve") < names.index("cline")


# Fake ``npm`` and ``node`` for the run command: npm leaves node_modules and a
# lockfile in its cwd, ``eve build`` leaves build output, and the runner copies
# the project it got to $RECORD_DIR, then edits main.py as the agent would.
_FAKE_NPM = """#!/bin/sh
if [ -n "$FAKE_NPM_FAILS" ]; then exit 1; fi
mkdir -p node_modules/.bin node_modules/eve
echo '{}' > package-lock.json
echo 'module.exports = {}' > node_modules/eve/index.js
printf '#!/bin/sh\\nmkdir -p .eve && echo built > .eve/server.mjs\\n' > node_modules/.bin/eve
chmod +x node_modules/.bin/eve
"""
_FAKE_NODE = """#!/bin/sh
cp -R "$EVE_PROJECT_DIR/." "$RECORD_DIR/"
echo "$EVE_PROJECT_DIR" > "$RECORD_DIR/.project_dir"
echo "$EVE_WORKSPACE" > "$RECORD_DIR/.workspace"
if [ -f main.py ]; then echo 'print(2)' > main.py; fi
"""


def _run_with_fakes(run_command: str, workspace: Path, tmp_path: Path, **env: str) -> Path:
    """Run the real eve run command with fake ``npm`` and ``node`` on PATH.

    Args:
        run_command: The launch run command.
        workspace: The run workspace, which holds the launch files.
        tmp_path: Test folder for the fakes, TMPDIR and the record.
        **env: Extra environment variables.

    Returns:
        The folder that holds a copy of the eve project the runner received.
    """
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    for name, body in (("npm", _FAKE_NPM), ("node", _FAKE_NODE)):
        (fakes / name).write_text(body)
        (fakes / name).chmod(0o755)
    record = tmp_path / "record"
    record.mkdir()
    temp = tmp_path / "tmp"
    temp.mkdir()
    subprocess.run(
        ["/bin/sh", "-c", run_command],
        cwd=workspace,
        env={
            "PATH": f"{fakes}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
            "HOME": str(tmp_path),
            "TMPDIR": str(temp),
            "RECORD_DIR": str(record),
            **env,
        },
        timeout=30,
        check=True,
    )
    return record


def _write_launch_files(launch_files: dict[str, str], instructions_file: str, root: Path) -> None:
    """Write the launch files and the instructions the way the bridge does.

    Args:
        launch_files: Workspace-relative path to content.
        instructions_file: Workspace-relative instructions path.
        root: The workspace.
    """
    for name, content in launch_files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (root / instructions_file).write_text("instructions\n")


def test_launch_generates_a_pinned_eve_project(tmp_path: Path) -> None:
    """Write package.json with exact pins, an agent on the gateway, and the runner."""
    launch = build_launch(_target(), _GATEWAY)
    assert launch.instructions_file == EVE_INSTRUCTIONS_FILE == ".app/eve/agent/instructions.md"
    assert set(launch.files) == {".app/eve/agent/agent.ts"}
    agent = launch.files[".app/eve/agent/agent.ts"]
    assert 'baseURL: "https://gw.example/v1"' in agent
    assert "apiKey: process.env.APP_API_KEY" in agent
    assert 'gateway("acme/agent-1")' in agent
    assert "secret-key" not in agent
    workspace = tmp_path / "work"
    workspace.mkdir()
    _write_launch_files(launch.files, launch.instructions_file, workspace)
    record = _run_with_fakes(launch.run_command, workspace, tmp_path)
    package = json.loads((record / "package.json").read_text())
    assert package["dependencies"] == {
        "@ai-sdk/openai-compatible": OPENAI_COMPATIBLE_VERSION,
        "ai": AI_SDK_VERSION,
        "eve": EVE_VERSION,
        "zod": ZOD_VERSION,
    }
    assert all(version[0].isdigit() for version in package["dependencies"].values())
    assert (record / "runner.mjs").read_text() == EVE_RUNNER
    assert (record / "agent/agent.ts").read_text() == agent
    assert (record / "agent/instructions.md").read_text() == "instructions\n"
    assert (record / "agent/sandbox.ts").read_text() == EVE_SANDBOX
    assert (record / "agent/channels/eve.ts").read_text() == EVE_CHANNEL
    assert (record / ".eve/server.mjs").is_file()
    project_dir = Path((record / ".project_dir").read_text().strip())
    assert project_dir.is_absolute()
    assert workspace.resolve() not in project_dir.resolve().parents
    assert Path((record / ".workspace").read_text().strip()).resolve() == workspace.resolve()
    assert not project_dir.exists()
    assert list((tmp_path / "tmp").iterdir()) == []
    assert sorted(str(path.relative_to(workspace)) for path in workspace.rglob("*") if path.is_file()) == [
        ".app/eve/agent/agent.ts",
        ".app/eve/agent/instructions.md",
    ]


def test_launch_installs_the_pinned_cli_and_runs_the_runner() -> None:
    """Install eve at the pin, build a temp project after the box files land, then run the runner."""
    launch = build_launch(_target(), _GATEWAY)
    assert launch.install_command == (
        "eve --version 2>/dev/null | grep -Eq '(^| )1[.]0[.]0-rc[.]6( |$)' || npm install -g eve@1.0.0-rc.6"
    )
    assert launch.run_command.startswith(
        'export EVE_WORKSPACE="$PWD" && EVE_DIR="$(mktemp -d)" && trap \'rm -rf "$EVE_DIR"\' EXIT && '
    )
    assert f'if [ -d "{EVE_IMAGE_DEPS}/eve" ]; then ' in launch.run_command
    assert "else npm install " in launch.run_command
    assert "node_modules/.bin/eve build) >&2 && " in launch.run_command
    assert launch.run_command.endswith('EVE_PROJECT_DIR="$EVE_DIR" node "$EVE_DIR/runner.mjs"')
    assert launch_payload(launch)["output_format"] == "pi"


def test_launch_links_the_preinstalled_image_packages_instead_of_installing(tmp_path: Path) -> None:
    """Link each preinstalled package into the project's own node_modules and never call npm."""
    deps = tmp_path / "image-deps"
    (deps / "eve").mkdir(parents=True)
    (deps / "@ai-sdk/openai-compatible").mkdir(parents=True)
    (deps / ".bin").mkdir()
    (deps / ".bin/eve").write_text("#!/bin/sh\nmkdir -p .eve && echo built > .eve/server.mjs\n")
    (deps / ".bin/eve").chmod(0o755)
    launch = build_launch(_target(), _GATEWAY)
    workspace = tmp_path / "work"
    workspace.mkdir()
    _write_launch_files(launch.files, launch.instructions_file, workspace)
    run_command = launch.run_command.replace(EVE_IMAGE_DEPS, str(deps))
    record = _run_with_fakes(run_command, workspace, tmp_path, FAKE_NPM_FAILS="1")
    modules = record / "node_modules"
    assert sorted(path.name for path in modules.iterdir()) == [".bin", "@ai-sdk", "eve"]
    assert (record / ".eve/server.mjs").is_file()
    assert not (deps / ".eve").exists()


def test_image_lock_pins_the_same_eve_packages_as_the_harness() -> None:
    """Preinstall eve and its peers in the sandbox image at the harness's exact pins."""
    package = json.loads((_SANDBOX_RUNTIME / "package.json").read_text())
    pins = {
        "@ai-sdk/openai-compatible": OPENAI_COMPATIBLE_VERSION,
        "ai": AI_SDK_VERSION,
        "eve": EVE_VERSION,
        "zod": ZOD_VERSION,
    }
    assert {name: package["dependencies"][name] for name in pins} == pins
    lock = json.loads((_SANDBOX_RUNTIME / "package-lock.json").read_text())["packages"]
    assert {name: lock[f"node_modules/{name}"]["version"] for name in pins} == pins
    assert all(lock[f"node_modules/{name}"].get("integrity") for name in pins)


@pytest.mark.parametrize(
    ("effort", "level"),
    [(None, None), ("none", "none"), ("minimal", "minimal"), ("high", "high"), ("max", "xhigh"), ("turbo", None)],
)
def test_reasoning_effort_reaches_the_agent_config(effort: str | None, level: str | None) -> None:
    """Pass the user's effort as eve's ``reasoning`` level, or leave the model's default."""
    agent = build_launch(_target(), _GATEWAY, reasoning_effort=effort).files[".app/eve/agent/agent.ts"]
    assert "modelContextWindowTokens: 200_000" in agent
    if level is None:
        assert "reasoning:" not in agent
    else:
        assert f'reasoning: "{level}"' in agent


def test_sandbox_runs_tools_in_the_run_workspace_without_the_gateway_key() -> None:
    """Give eve's tools a host sandbox rooted at the run workspace that hides the key and runner token."""
    assert 'defineSandboxProvider({ name: "skynet-workspace"' in EVE_SANDBOX
    assert "process.env.EVE_WORKSPACE" in EVE_SANDBOX
    assert "export const environment = " in EVE_SANDBOX
    assert "export default defineSandbox(" in EVE_SANDBOX
    for name in ("APP_API_KEY", "EVE_RUNNER_TOKEN"):
        assert f'"{name}"' in EVE_SANDBOX
    assert "eveChannel({ auth: [runnerToken] })" in EVE_CHANNEL
    assert "process.env.EVE_RUNNER_TOKEN" in EVE_CHANNEL
    assert "timingSafeEqual" in EVE_CHANNEL
    assert "env.EVE_RUNNER_TOKEN = runnerToken;" in EVE_RUNNER
    assert "auth: { bearer: runnerToken }" in EVE_RUNNER


def test_env_carries_only_the_gateway_and_turns_telemetry_off() -> None:
    """Give eve the gateway URL and key, no provider key, and telemetry off."""
    env = build_launch(_target(), _GATEWAY).env
    assert env["APP_GATEWAY_URL"] == "https://gw.example/v1"
    assert env["APP_API_KEY"] == "secret-key"
    assert env["APP_MODEL"] == "acme/agent-1"
    assert env["EVE_TELEMETRY_DISABLED"] == "1"
    assert env["NPM_CONFIG_UPDATE_NOTIFIER"] == "false"
    assert not set(_PROVIDER_KEYS) & set(env)


def test_runner_drops_direct_provider_keys_from_the_eve_server() -> None:
    """Delete every direct provider credential before the runner starts ``eve start``."""
    for key in _PROVIDER_KEYS:
        assert f'"{key}"' in EVE_RUNNER
    assert "for (const key of PROVIDER_ENV_KEYS) delete env[key];" in EVE_RUNNER
    assert '"start", "--host", host, "--port"' in EVE_RUNNER
    assert 'stdio: ["ignore", "pipe", "inherit"]' in EVE_RUNNER


def test_protected_launch_checks_the_pin_relays_the_gateway_and_blocks_updates(tmp_path: Path) -> None:
    """Probe the pinned CLI, rewrite the agent's gateway URL to the relay, and add the protected env."""
    launch = build_launch(_target(), _GATEWAY, protected=True)
    assert launch.install_command == pinned_harness_check("eve")
    assert "npm install -g" not in launch.install_command
    workspace = tmp_path / "work"
    workspace.mkdir()
    _write_launch_files(launch.files, launch.instructions_file, workspace)
    record = _run_with_fakes(launch.run_command, workspace, tmp_path, APP_BUDGET_RELAY_URL="http://127.0.0.1:3210/v1")
    assert 'baseURL: "http://127.0.0.1:3210/v1"' in (workspace / ".app/eve/agent/agent.ts").read_text()
    assert 'baseURL: "http://127.0.0.1:3210/v1"' in (record / "agent/agent.ts").read_text()
    assert launch.env["NPM_CONFIG_OFFLINE"] == "true"
    assert launch.env["EVE_TELEMETRY_DISABLED"] == "1"
    assert not set(_PROVIDER_KEYS) & set(launch.env)


def test_pi_parser_reads_the_recorded_runner_sample() -> None:
    """Read the answer and usage from the runner's Pi-shaped lines and skip plain log lines."""
    expected = ("The answer is 42.", {"input_tokens": 1200, "output_tokens": 85})
    assert parse_pi_output(_RUNNER_SAMPLE) == expected
    assert parse_output("pi", _RUNNER_SAMPLE) == expected


def test_tool_counter_counts_each_requested_eve_action() -> None:
    """Count one tool call per runner ``tool_execution_start`` line."""
    ids = [
        call_id
        for line in _RUNNER_SAMPLE.splitlines()
        if line.startswith("{")
        for call_id in tool_call_ids("pi", json.loads(line))
    ]
    assert ids == ["act_1", "act_2"]


def _shipped_tree(tmp_path: Path) -> Path:
    """Unpack a one-file shipped tree the way a repo-mode run does.

    Args:
        tmp_path: Test folder.

    Returns:
        The checkout.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tree:
        data = b"print(1)\n"
        info = tarfile.TarInfo("main.py")
        info.size = len(data)
        tree.addfile(info, io.BytesIO(data))
    chunk = tmp_path / "chunk0"
    chunk.write_text(base64.b64encode(buffer.getvalue()).decode("ascii"))
    return repo_tree.unpack_tree([chunk], tmp_path / "repo")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_version_patch_holds_only_the_code_change(tmp_path: Path) -> None:
    """Keep the eve project, its node_modules, lockfile and build output out of a repo-mode version patch."""
    checkout = _shipped_tree(tmp_path)
    payload = launch_payload(build_launch(_target(), _GATEWAY))
    repo_tree.exclude_paths(checkout, workspace_files(payload))
    _write_launch_files(payload["files"], payload["instructions_file"], checkout)

    _run_with_fakes(payload["run_command"], checkout, tmp_path)

    patch = repo_tree.version_patch(checkout)
    assert [line for line in patch.splitlines() if line.startswith("diff --git")] == ["diff --git a/main.py b/main.py"]
    assert "+print(2)" in patch


# A fake ``eve`` package for the real runner: ``eve start`` answers the health
# check, and the client streams the events that $FAKE_EVE_SCENARIO names.
_FAKE_EVE_SERVER = """#!/usr/bin/env node
import { createServer } from "node:http";
const port = Number(process.argv[process.argv.indexOf("--port") + 1]);
createServer((request, response) => response.end("ok")).listen(port, "127.0.0.1");
"""
_FAKE_EVE_CLIENT = """
const SCENARIOS = {
  done: [
    { type: "actions.requested", data: { actions: [{ id: "a1" }] } },
    { type: "call.requested", data: { callId: "c9" } },
    { type: "step.completed", data: { usage: { inputTokens: 10, outputTokens: 2 } } },
    { type: "usage.recorded", data: { inputTokens: 99, outputTokens: 99 } },
  ],
  tools: [
    { type: "call.requested", data: { callId: "c1" } },
    { type: "call.requested", data: { callId: "c2" } },
  ],
  cost: [{ type: "usage.recorded", data: { inputTokens: 5000, outputTokens: 0 } }],
};
const hang = () => new Promise(() => setInterval(() => {}, 1000));
export class Client {
  constructor(options) {
    if (!options?.baseUrl || !options?.auth?.bearer) throw new Error("missing baseUrl or bearer");
  }
  get sessions() {
    return {
      async create({ message }) {
        if (message !== "the prompt") throw new Error("wrong prompt");
        const scenario = process.env.FAKE_EVE_SCENARIO;
        return {
          session: { id: "s1" },
          delivery: {
            async *stream() {
              for (const event of SCENARIOS[scenario]) yield event;
              if (scenario !== "done") await hang();
            },
            async result() {
              return { status: "completed", reply: "done" };
            },
          },
        };
      },
    };
  }
}
"""


def _fake_eve_project(tmp_path: Path) -> Path:
    """Write an eve project whose server and client are local fakes.

    Args:
        tmp_path: Test folder.

    Returns:
        The project folder, with the real runner in it.
    """
    project = tmp_path / "eve"
    package = project / "node_modules/eve"
    tmp_path.mkdir(parents=True, exist_ok=True)
    package.mkdir(parents=True)
    (project / "package.json").write_text('{"type": "module"}')
    (project / "runner.mjs").write_text(EVE_RUNNER)
    (package / "package.json").write_text('{"name": "eve", "type": "module", "exports": {"./client": "./client.mjs"}}')
    (package / "client.mjs").write_text(_FAKE_EVE_CLIENT)
    server = project / "node_modules/.bin/eve"
    server.parent.mkdir()
    server.write_text(_FAKE_EVE_SERVER)
    server.chmod(0o755)
    (tmp_path / "prompt.txt").write_text("the prompt")
    return project


def _monitor_runner(tmp_path: Path, scenario: str, **caps: object) -> _Monitor:
    """Run the real runner against the fake eve under the bridge monitor.

    Args:
        tmp_path: Test folder.
        scenario: The fake client's event list.
        **caps: ``price``, ``max_cost_usd`` and ``max_tool_calls`` for the monitor.

    Returns:
        The monitor after the runner exits.
    """
    project = _fake_eve_project(tmp_path)
    process = subprocess.Popen(
        ["node", str(project / "runner.mjs")],
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "EVE_PROJECT_DIR": str(project),
            "APP_PROMPT_FILE": str(tmp_path / "prompt.txt"),
            "FAKE_EVE_SCENARIO": scenario,
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    monitor = _Monitor(
        process,
        "pi",
        caps.get("price"),  # type: ignore[arg-type]
        caps.get("max_cost_usd"),  # type: ignore[arg-type]
        caps.get("max_tool_calls"),  # type: ignore[arg-type]
    )
    reader = threading.Thread(target=monitor.pump, daemon=True)
    reader.start()
    try:
        process.wait(timeout=30)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait()
    reader.join(timeout=5)
    return monitor


# eve start listens on a fixed port, so the runner scenarios share one test
# and never run at the same time on parallel workers.
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_runner_streams_events_so_the_caps_stop_a_running_session(tmp_path: Path) -> None:
    """Stream calls and usage once per event family, and let both caps stop a turn that never ends."""
    done = _monitor_runner(tmp_path / "done", "done")
    stdout = "".join(done.chunks)
    assert done.process.returncode == 0
    ids = [call_id for line in stdout.splitlines() for call_id in tool_call_ids("pi", json.loads(line))]
    assert ids == ["a1"]
    assert parse_pi_output(stdout) == ("done", {"input_tokens": 10, "output_tokens": 2})

    tools = _monitor_runner(tmp_path / "tools", "tools", max_tool_calls=1)
    assert tools.tool_cap_reached
    assert tools.tool_calls == 2
    assert tools.process.returncode != 0

    cost = _monitor_runner(tmp_path / "cost", "cost", price={"input": 0.001, "output": 0.0}, max_cost_usd=1.0)
    assert cost.budget_exceeded
    assert cost.process.returncode != 0
