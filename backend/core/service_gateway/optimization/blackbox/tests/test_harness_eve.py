"""Tests for the eve harness: registration, the generated project, gateway-only env and the runner output."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import tarfile
import threading
import time
from pathlib import Path

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESS_EVE, BLACKBOX_HARNESSES, BLACKBOX_TARGET_AGENT, BlackboxTarget

from .. import repo_tree
from ..harness import GatewayConfig, build_launch, launch_payload, pinned_harness_check
from ..harness_bridge import (
    GUARD_REFUSAL,
    _Monitor,
    install_guard,
    parse_output,
    parse_pi_output,
    tool_call_ids,
    workspace_files,
)
from ..harnesses import REGISTRY
from ..harnesses.eve import (
    _NODE_CHECK,
    AI_SDK_VERSION,
    EVE_CHANNEL,
    EVE_DISABLE_AGENT_TOOL,
    EVE_IMAGE_DEPS,
    EVE_INSTRUCTIONS_FILE,
    EVE_REASONING_LEVELS,
    EVE_RUNNER,
    EVE_SANDBOX,
    EVE_VERSION,
    OPENAI_COMPATIBLE_VERSION,
    ZOD_VERSION,
)

_SANDBOX_RUNTIME = Path(__file__).resolve().parents[5] / "sandbox-runtime"
_NODE = shutil.which("node")

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
# ``node -e`` is the Node version check, which passes unless $FAKE_NODE_OLD is set.
_FAKE_NPM = """#!/bin/sh
if [ -n "$FAKE_NPM_FAILS" ]; then exit 1; fi
mkdir -p node_modules/.bin node_modules/eve
echo '{}' > package-lock.json
echo 'module.exports = {}' > node_modules/eve/index.js
printf '#!/bin/sh\\nmkdir -p .eve && echo built > .eve/server.mjs\\n' > node_modules/.bin/eve
chmod +x node_modules/.bin/eve
"""
_FAKE_NODE = """#!/bin/sh
if [ "$1" = "-e" ]; then
  if [ -n "$FAKE_NODE_OLD" ]; then echo "eve needs Node 24 or newer; found v22.0.0" >&2; exit 1; fi
  exit 0
fi
cp -R "$EVE_PROJECT_DIR/." "$RECORD_DIR/"
echo "$EVE_PROJECT_DIR" > "$RECORD_DIR/.project_dir"
echo "$EVE_WORKSPACE" > "$RECORD_DIR/.workspace"
if [ -f main.py ]; then echo 'print(2)' > main.py; fi
"""


def _run_with_fakes(run_command: str, workspace: Path, tmp_path: Path, *, check: bool = True, **env: str) -> Path:
    """Run the real eve run command with fake ``npm`` and ``node`` on PATH.

    Args:
        run_command: The launch run command.
        workspace: The run workspace, which holds the launch files.
        tmp_path: Test folder for the fakes, TMPDIR and the record.
        check: Fail the test when the run command exits non-zero.
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
        check=check,
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
    assert (record / "agent/tools/agent.ts").read_text() == EVE_DISABLE_AGENT_TOOL
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


def test_reasoning_levels_match_eve() -> None:
    """Offer exactly eve's own levels, without ``provider-default``, which no effort already means."""
    assert EVE_REASONING_LEVELS == ("none", "minimal", "low", "medium", "high", "xhigh")


@pytest.mark.parametrize("effort", [None, *EVE_REASONING_LEVELS])
def test_reasoning_effort_reaches_the_agent_config_unchanged(effort: str | None) -> None:
    """Pass each eve level unchanged as ``reasoning``, or leave the model's default."""
    agent = build_launch(_target(), _GATEWAY, reasoning_effort=effort).files[".app/eve/agent/agent.ts"]
    assert "modelContextWindowTokens: 200_000" in agent
    if effort is None:
        assert "reasoning:" not in agent
    else:
        assert f'reasoning: "{effort}"' in agent


@pytest.mark.parametrize("effort", ["max", "turbo", "provider-default", "HIGH"])
def test_reasoning_effort_that_eve_lacks_is_refused(effort: str) -> None:
    """Refuse a level eve does not have, with its name, instead of a silent remap or drop."""
    with pytest.raises(ServiceError, match=f"does not support the effort '{effort}'"):
        build_launch(_target(), _GATEWAY, reasoning_effort=effort)


def test_sandbox_runs_tools_in_the_run_workspace_without_the_gateway_key() -> None:
    """Give eve's tools a host sandbox rooted at the run workspace that hides the key and runner token."""
    assert 'defineSandboxProvider({ name: "skynet-workspace"' in EVE_SANDBOX
    assert "process.env.EVE_WORKSPACE" in EVE_SANDBOX
    assert "export const environment = " in EVE_SANDBOX
    assert "export default defineSandbox(" in EVE_SANDBOX
    for name in ("APP_API_KEY", "EVE_RUNNER_TOKEN"):
        assert f'"{name}"' in EVE_SANDBOX
    assert "process.env.APP_GUARD_EVE" in EVE_SANDBOX
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
    # A protected run blanks the direct provider credentials, like goose.
    credentials = [key for key in _PROVIDER_KEYS if key != "OPENAI_BASE_URL"]
    assert {key: launch.env.get(key) for key in credentials} == dict.fromkeys(credentials, "")
    assert "OPENAI_BASE_URL" not in launch.env


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


# A fake ``eve`` package for the real runner: ``eve start`` answers the
# health check without auth and the info route only with the runner token
# (never with $FAKE_EVE_REFUSE set), and the client streams the protocol v27
# events that $FAKE_EVE_SCENARIO names.
_FAKE_EVE_SERVER = """#!/usr/bin/env node
import { createServer } from "node:http";
const port = Number(process.argv[process.argv.indexOf("--port") + 1]);
const bearer = "Bearer " + process.env.EVE_RUNNER_TOKEN;
createServer((request, response) => {
  if (request.url === "/eve/v1/health") return response.end("ok");
  const accepted = !process.env.FAKE_EVE_REFUSE && request.headers.authorization === bearer;
  response.statusCode = accepted ? 200 : 401;
  response.end(accepted ? "{}" : "unauthorized");
}).listen(port, "127.0.0.1");
"""
_FAKE_EVE_CLIENT = """
const SCENARIOS = {
  done: [
    { type: "call.requested", data: { callId: "c1" } },
    { type: "usage.recorded", data: { usage: { inputTokens: 10, outputTokens: 2, cacheReadTokens: 0 } } },
    { type: "turn.settled", data: { outcome: "completed" } },
  ],
  failed: [{ type: "turn.settled", data: { outcome: "failed", error: { message: "the model call failed" } } }],
  tools: [
    { type: "call.requested", data: { callId: "c1" } },
    { type: "call.requested", data: { callId: "c2" } },
  ],
  cost: [{ type: "usage.recorded", data: { usage: { inputTokens: 5000, outputTokens: 0 } } }],
  silent: [{ type: "turn.settled", data: { outcome: "failed", error: { message: "" } } }],
  waiting: [],
};
const RESULTS = { waiting: { status: "awaiting-input" } };
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
              if (scenario === "tools" || scenario === "cost") await hang();
            },
            async result() {
              return RESULTS[scenario] ?? { status: "completed", reply: "done" };
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


def _monitor_runner(tmp_path: Path, scenario: str, env: dict[str, str] | None = None, **caps: object) -> _Monitor:
    """Run the real runner against the fake eve under the bridge monitor.

    Args:
        tmp_path: Test folder.
        scenario: The fake client's event list.
        env: Extra environment variables for the runner and the fake server.
        **caps: ``price``, ``max_cost_usd`` and ``max_tool_calls`` for the monitor.

    Returns:
        The monitor after the runner exits; its ``stderr`` attribute holds the runner's stderr.
    """
    project = _fake_eve_project(tmp_path)
    stderr = tmp_path / "stderr.txt"
    with stderr.open("w") as stderr_file:
        process = subprocess.Popen(
            ["node", str(project / "runner.mjs")],
            cwd=tmp_path,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "EVE_PROJECT_DIR": str(project),
                "APP_PROMPT_FILE": str(tmp_path / "prompt.txt"),
                "FAKE_EVE_SCENARIO": scenario,
                **(env or {}),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
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
    monitor.stderr = stderr.read_text()  # type: ignore[attr-defined]
    return monitor


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_runner_streams_events_so_the_caps_stop_a_running_session(tmp_path: Path) -> None:
    """Stream v27 calls and usage, fail a failed turn, and let both caps stop a turn that never ends."""
    runs: dict[str, _Monitor] = {}
    # Two runners at once each pick their own free port.
    threads = [
        threading.Thread(target=lambda name=name: runs.__setitem__(name, _monitor_runner(tmp_path / name, "done")))
        for name in ("done", "done2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for done in runs.values():
        stdout = "".join(done.chunks)
        assert done.process.returncode == 0
        ids = [call_id for line in stdout.splitlines() for call_id in tool_call_ids("pi", json.loads(line))]
        assert ids == ["c1"]
        assert parse_pi_output(stdout) == ("done", {"input_tokens": 10, "output_tokens": 2})

    failed = _monitor_runner(tmp_path / "failed", "failed")
    assert failed.process.returncode != 0
    assert "the model call failed" in failed.stderr  # type: ignore[attr-defined]

    silent = _monitor_runner(tmp_path / "silent", "silent")
    assert silent.process.returncode != 0
    assert "turn.settled ended failed during the eve run" in silent.stderr  # type: ignore[attr-defined]

    waiting = _monitor_runner(tmp_path / "waiting", "waiting")
    assert waiting.process.returncode != 0
    assert "eve run ended with status awaiting-input" in waiting.stderr  # type: ignore[attr-defined]

    tools = _monitor_runner(tmp_path / "tools", "tools", max_tool_calls=1)
    assert tools.tool_cap_reached
    assert tools.tool_calls == 2
    assert tools.process.returncode != 0

    cost = _monitor_runner(tmp_path / "cost", "cost", price={"input": 0.001, "output": 0.0}, max_cost_usd=1.0)
    assert cost.budget_exceeded
    assert cost.process.returncode != 0


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_runner_stops_when_the_server_on_its_port_refuses_the_token(tmp_path: Path) -> None:
    """Stop before the prompt when the server that answers health does not accept the runner token."""
    refused = _monitor_runner(tmp_path, "done", {"FAKE_EVE_REFUSE": "1"})
    assert refused.process.returncode != 0
    assert "refused the runner token (401)" in refused.stderr  # type: ignore[attr-defined]
    assert "".join(refused.chunks) == ""


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_node_check_needs_node_24_or_newer() -> None:
    """Pass the setup Node check on Node 24 or newer and stop older Node with a clear message."""
    completed = subprocess.run(["sh", "-c", _NODE_CHECK], capture_output=True, text=True, check=False)
    version = subprocess.run([_NODE, "-p", "process.versions.node"], capture_output=True, text=True, check=True)
    major = int(version.stdout.split(".")[0])
    if major >= 24:
        assert completed.returncode == 0
    else:
        assert completed.returncode == 1
        assert "eve needs Node 24 or newer" in completed.stderr


def test_launch_stops_on_old_node_before_the_eve_build(tmp_path: Path) -> None:
    """Stop the run command before the install, the build and the runner when the Node check fails."""
    launch = build_launch(_target(), _GATEWAY)
    workspace = tmp_path / "work"
    workspace.mkdir()
    _write_launch_files(launch.files, launch.instructions_file, workspace)
    record = _run_with_fakes(launch.run_command, workspace, tmp_path, check=False, FAKE_NODE_OLD="1")
    assert not any(record.iterdir())


# Stubs of the two eve entry points that the sandbox module imports, so the
# test drives the real provider code without the eve package.
_SANDBOX_STUBS = {
    "package.json": json.dumps(
        {
            "name": "eve",
            "type": "module",
            "exports": {"./sandbox": "./sandbox.mjs", "./sandbox/provider": "./provider.mjs"},
        }
    ),
    "sandbox.mjs": "export const defineSandbox = (open) => open;\n",
    "provider.mjs": (
        "export const SANDBOX_AGENT_ENV = {};\n"
        "export const defineSandboxProvider = (definition) => ({ environment: () => definition.environment() });\n"
    ),
}
_SANDBOX_DRIVER = """
const { environment } = await import(process.argv[1]);
const { connection } = await environment.start();
const run = async (command) => {
  const child = await connection.spawn({ command, env: {}, workingDirectory: process.env.EVE_WORKSPACE });
  const [stdout] = await Promise.all([new Response(child.stdout).text(), child.wait()]);
  return stdout;
};
const refusal = async (call) => {
  try {
    await call();
    return null;
  } catch (error) {
    return error.message;
  }
};
const workspace = process.env.EVE_WORKSPACE;
console.log(JSON.stringify({
  env: await run("env"),
  path: await run('printf %s "$PATH"'),
  readRefused: await refusal(() => connection.readFile({ path: workspace + "/optimize.toml" })),
  bashRefused: await refusal(() => run("cat optimize.toml")),
  writeRefused: await refusal(() => connection.writeFiles({ files: [{ path: workspace + "/optimize.toml", content: "x" }] })),
  removeRefused: await refusal(() => connection.removePath({ path: workspace + "/optimize.toml", force: true })),
  allowed: new TextDecoder().decode(await connection.readFile({ path: workspace + "/main.py" })),
}));
"""


_BOX_TOKENS = ("CANDIDATE_TOKEN", "HOST_AGENT_TOKEN", "PACKAGE_RELAY_TOKEN", "TOOL_RELAY_TOKEN")


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_sandbox_hides_the_server_secrets_and_asks_the_guard_for_every_tool(tmp_path: Path) -> None:
    """Run the real sandbox provider: drop the key and token from commands, keep PATH, and refuse hidden paths."""
    project = tmp_path / "eve"
    stubs = project / "node_modules/eve"
    stubs.mkdir(parents=True)
    for name, text in _SANDBOX_STUBS.items():
        (stubs / name).write_text(text)
    (project / "sandbox.mjs").write_text(EVE_SANDBOX)
    workspace = tmp_path / "work"
    workspace.mkdir()
    tools_bin = tmp_path / "bin"
    tools_bin.mkdir()
    (workspace / "optimize.toml").write_text("hidden = true\n")
    (workspace / "main.py").write_text("print(1)\n")
    guard_env = install_guard(tmp_path / "session", workspace, ["optimize.toml"])

    completed = subprocess.run(
        [_NODE, "--input-type=module", "-e", _SANDBOX_DRIVER, str((project / "sandbox.mjs").as_uri())],
        cwd=project,
        env={
            "PATH": f"{tools_bin}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
            "APP_API_KEY": "secret-key",
            "SKYNET_API_KEY": "secret-key",
            "EVE_RUNNER_TOKEN": "runner-token",
            "EVE_PROJECT_DIR": str(project),
            "EVE_WORKSPACE": str(workspace),
            **{f"{prefix}_{name}": "box-token" for prefix in ("APP", "SKYNET") for name in _BOX_TOKENS},
            "APP_GUARD_EVE": guard_env["APP_GUARD_EVE"],
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(completed.stdout)
    names = {line.split("=", 1)[0] for line in result["env"].splitlines()}
    assert not names & {"APP_API_KEY", "SKYNET_API_KEY", "EVE_RUNNER_TOKEN", "EVE_PROJECT_DIR", "EVE_WORKSPACE"}
    assert not {name for name in names if name.endswith(_BOX_TOKENS)}
    # A login shell reads /etc/profile, which resets PATH on Debian images and
    # would drop /opt/node24/bin and the venv from every tool command.
    assert result["path"].split(os.pathsep)[0] == str(tools_bin)
    for key in ("readRefused", "bashRefused", "writeRefused", "removeRefused"):
        assert result[key] == GUARD_REFUSAL, key
    assert result["allowed"] == "print(1)\n"
    assert (workspace / "optimize.toml").read_text() == "hidden = true\n"


# Starts a command whose background ``sleep`` holds the stdout pipe, reads the
# sleep's pid from the first line, then stops the command through ``kill()``
# or through the abort signal and waits for it to end.
_KILL_DRIVER = """
const { environment } = await import(process.argv[1]);
const { connection } = await environment.start();
const stop = async (how) => {
  const controller = new AbortController();
  const child = await connection.spawn({
    command: "sleep 60 & echo $!; wait",
    env: {},
    signal: controller.signal,
    workingDirectory: process.env.EVE_WORKSPACE,
  });
  const reader = child.stdout.getReader();
  const { value } = await reader.read();
  const pid = Number(new TextDecoder().decode(value).trim());
  if (how === "kill") await child.kill();
  else controller.abort();
  const ended = await Promise.race([
    child.wait().then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 10000)),
  ]);
  await reader.cancel();
  return { pid, ended };
};
console.log(JSON.stringify({ kill: await stop("kill"), abort: await stop("abort") }));
process.exit(0);
"""


def _running(pid: int) -> bool:
    """Tell whether a process runs and is not a zombie.

    Args:
        pid: Process id.

    Returns:
        ``True`` while ``ps`` lists the process in a state other than zombie.
    """
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return bool(state.stdout.strip()) and not state.stdout.strip().startswith("Z")


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
def test_sandbox_kill_and_abort_stop_the_processes_a_command_started(tmp_path: Path) -> None:
    """Kill the whole process group of a tool command, so a child it started dies with it."""
    project = tmp_path / "eve"
    stubs = project / "node_modules/eve"
    stubs.mkdir(parents=True)
    for name, text in _SANDBOX_STUBS.items():
        (stubs / name).write_text(text)
    (project / "sandbox.mjs").write_text(EVE_SANDBOX)
    workspace = tmp_path / "work"
    workspace.mkdir()

    completed = subprocess.run(
        [_NODE, "--input-type=module", "-e", _KILL_DRIVER, str((project / "sandbox.mjs").as_uri())],
        cwd=project,
        env={"PATH": os.environ.get("PATH", os.defpath), "EVE_WORKSPACE": str(workspace)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    result = json.loads(completed.stdout)
    for how in ("kill", "abort"):
        pid = result[how]["pid"]
        try:
            # The sleep holds stdout, so the command ends only once it dies.
            assert result[how]["ended"], f"{how}: the command did not end"
            deadline = time.monotonic() + 10
            while _running(pid):
                assert time.monotonic() < deadline, f"{how}: the child process outlived the command"
                time.sleep(0.05)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
