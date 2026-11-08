"""eve harness (npm ``eve``), a filesystem-first agent framework run through its local HTTP session API.

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/eve.py and
agents/installed/eve_runner.mjs. Skynet generates the eve project itself
instead of copying a user project, and the runner prints Pi-shaped JSON lines
so the bridge's Pi parser reads the answer, the usage and the tool calls.
"""

from __future__ import annotations

import json
import shlex

from .....models.blackbox import BLACKBOX_HARNESS_EVE
from ..harness_bridge import parse_pi_output
from . import HarnessSpec, register
from .base import PROVIDER, GatewayConfig, HarnessLaunch, _base_env, _npm_install

EVE_VERSION = "1.0.0-rc.6"
# eve takes ``ai`` as a peer dependency, and the gateway provider needs zod as
# its peer; every package is exact so each box runs the same agent code.
AI_SDK_VERSION = "7.0.165"
OPENAI_COMPATIBLE_VERSION = "3.0.87"
ZOD_VERSION = "4.6.7"
EVE_PROJECT_DIR = ".app/eve"
EVE_INSTRUCTIONS_FILE = f"{EVE_PROJECT_DIR}/agent/instructions.md"
_AGENT_FILE = f"{EVE_PROJECT_DIR}/agent/agent.ts"
_NPM_FLAGS = "--no-fund --no-audit --loglevel=error --progress=false"
_ENV = {
    "EVE_TELEMETRY_DISABLED": "1",
    "NPM_CONFIG_UPDATE_NOTIFIER": "false",
}
# A protected sandbox has no route to the internet, so the project install
# must come from the npm cache and fail fast instead of waiting on the registry.
_PROTECTED_ENV = {
    "EVE_TELEMETRY_DISABLED": "1",
    "NPM_CONFIG_UPDATE_NOTIFIER": "false",
    "NPM_CONFIG_OFFLINE": "true",
}

# The runner starts ``eve start``, sends the case prompt as one session turn
# and prints Pi's ``tool_execution_start`` and ``message_end`` events as the
# session streams them, so the bridge's tool-call and cost caps can stop a
# running session. It accepts both the older event names (``actions.requested``,
# ``step.completed``) and the newer ones (``call.requested``,
# ``usage.recorded``), and counts only the first name family it sees so a
# stream that sends both is not counted twice. It drops every direct provider
# credential from the server env, so the AI SDK can only reach the model
# through the Skynet gateway that agent.ts names.
EVE_RUNNER = r"""import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const PROVIDER_ENV_KEYS = [
  "AI_GATEWAY_API_KEY", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_DEFAULT_REGION",
  "AWS_REGION", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AZURE_API_KEY",
  "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_RESOURCE_NAME", "DEEPSEEK_API_KEY",
  "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_LOCATION",
  "GOOGLE_CLOUD_PROJECT", "GOOGLE_GENERATIVE_AI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
  "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENROUTER_API_KEY", "VERCEL_OIDC_TOKEN", "XAI_API_KEY",
];
const OK_STATUSES = new Set(["waiting", "completed", "session.waiting", "session.completed"]);
const projectDir = process.env.EVE_PROJECT_DIR;
const host = "127.0.0.1";
const port = 3024;
const serverUrl = `http://${host}:${port}`;
const startupTimeoutMs = 60000;

const FAILURE_TYPES = new Set(["turn.failed", "session.failed"]);
const INPUT_TYPES = new Set(["input.requested", "interaction.opened"]);
const SETTLED_TYPES = new Set(["turn.settled", "session.ended"]);
const counted = { calls: null, usage: null };
let failure = null;

let server;
try {
  server = startServer();
  await waitForHealth(server);
  const prompt = await readFile(process.env.APP_PROMPT_FILE, "utf8");
  const require = createRequire(join(projectDir, "package.json"));
  const { Client } = await import(pathToFileURL(require.resolve("eve/client")).href);
  const response = await new Client({ host: serverUrl }).session().send(prompt);
  const streamed = typeof response.stream === "function";
  if (streamed) {
    for await (const event of response.stream()) handleEvent(event);
  }
  const result = await response.result();
  if (!streamed) for (const event of result.events ?? []) handleEvent(event);
  printMessage(messageText(result.message), { input: 0, output: 0 });
  failure ??= OK_STATUSES.has(result.status) ? null : `eve run ended with status ${result.status ?? "unknown"}`;
  if (failure) throw new Error(failure);
} finally {
  if (server) await stopServer(server);
}

function handleEvent(event) {
  const type = event?.type;
  const data = event?.data ?? {};
  if (type === "actions.requested" || type === "call.requested") {
    counted.calls ??= type;
    if (type !== counted.calls) return;
    const calls = type === "call.requested" ? [data] : actionsOf(data);
    for (const call of calls) {
      const id = call.callId ?? call.id ?? call.toolCallId ?? call.actionId ?? "";
      console.log(JSON.stringify({ type: "tool_execution_start", toolCallId: String(id) }));
    }
  } else if (type === "step.completed" || type === "usage.recorded") {
    counted.usage ??= type;
    if (type !== counted.usage) return;
    const source = data.usage ?? data;
    printMessage("", {
      input: firstNumber(source.inputTokens, source.input_tokens, source.promptTokens, source.prompt_tokens),
      output: firstNumber(source.outputTokens, source.output_tokens, source.completionTokens, source.completion_tokens),
    });
  } else if (INPUT_TYPES.has(type)) {
    failure ??= "eve asked for human input during a non-interactive run";
  } else if (FAILURE_TYPES.has(type) || (SETTLED_TYPES.has(type) && ["failed", "cancelled"].includes(data.outcome))) {
    failure ??= data.message ?? data.error?.message ?? `${type} during the eve run`;
  }
}

function printMessage(text, usage) {
  console.log(JSON.stringify({ type: "message_end", message: { role: "assistant", content: [{ type: "text", text }], usage } }));
}

function startServer() {
  const env = { ...process.env };
  for (const key of PROVIDER_ENV_KEYS) delete env[key];
  const child = spawn(join(projectDir, "node_modules", ".bin", "eve"), ["start", "--host", host, "--port", String(port)], {
    cwd: projectDir,
    env,
    stdio: ["ignore", "pipe", "inherit"],
  });
  // stdout is the event stream the bridge parses, so server logs go to stderr.
  child.stdout?.pipe(process.stderr);
  return child;
}

async function waitForHealth(child) {
  const deadline = Date.now() + startupTimeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`eve start exited before it was healthy (code ${child.exitCode})`);
    try {
      const response = await fetch(`${serverUrl}/eve/v1/health`);
      if (response.ok) return;
      lastError = new Error(`health returned ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`timed out waiting for eve start: ${lastError?.message ?? "unknown"}`);
}

function actionsOf(data) {
  for (const key of ["actions", "toolCalls", "tool_calls", "requests"]) {
    if (Array.isArray(data[key])) return data[key].filter((item) => item && typeof item === "object");
  }
  return [];
}

function messageText(message) {
  if (typeof message === "string") return message;
  if (Array.isArray(message)) return message.filter((part) => part?.type === "text").map((part) => part.text ?? "").join("\n");
  if (message && typeof message === "object") return messageText(message.text ?? message.content ?? "");
  return "";
}

function firstNumber(...values) {
  return values.find((value) => typeof value === "number" && Number.isFinite(value)) ?? 0;
}

async function stopServer(child) {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  const exited = await Promise.race([
    new Promise((resolve) => child.once("exit", () => resolve(true))),
    new Promise((resolve) => setTimeout(() => resolve(false), 5000)),
  ]);
  if (!exited && child.exitCode === null) child.kill("SIGKILL");
}
"""


def _agent_source(model: str, gateway: GatewayConfig) -> str:
    """Return the eve ``agent/agent.ts`` that sends every model call to the Skynet gateway.

    The gateway URL is a literal so a protected run's relay rewrite finds it;
    the key stays in the environment and never lands in a file.

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key.

    Returns:
        The TypeScript source.
    """
    return (
        'import { createOpenAICompatible } from "@ai-sdk/openai-compatible";\n'
        'import { defineAgent } from "eve";\n\n'
        "const gateway = createOpenAICompatible({\n"
        f"  name: {json.dumps(PROVIDER)},\n"
        f"  baseURL: {json.dumps(gateway.url)},\n"
        "  apiKey: process.env.APP_API_KEY,\n"
        "  includeUsage: true,\n"
        "});\n\n"
        f"export default defineAgent({{ model: gateway({json.dumps(model)}) }});\n"
    )


def _package_json() -> str:
    """Return the eve project's ``package.json`` with every dependency pinned exactly.

    Returns:
        The JSON text.
    """
    package = {
        "name": "app-eve-harness",
        "private": True,
        "type": "module",
        "dependencies": {
            "@ai-sdk/openai-compatible": OPENAI_COMPATIBLE_VERSION,
            "ai": AI_SDK_VERSION,
            "eve": EVE_VERSION,
            "zod": ZOD_VERSION,
        },
    }
    return json.dumps(package, indent=2)


def _eve_launch(model: str, gateway: GatewayConfig, *, reasoning_effort: str | None = None) -> HarnessLaunch:
    """Build the launch for eve (npm ``eve``, pinned to :data:`EVE_VERSION`).

    The version under optimization becomes the agent's ``instructions.md``.
    The run copies agent.ts and the instructions into a fresh ``mktemp -d``
    project outside the workspace, then installs and builds it there, since
    ``eve build`` compiles those instructions. So package.json, the runner,
    node_modules and the build output never reach the version patch. The
    runner then sends the case prompt.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: Accepted for the launch signature; eve's agent config
            gets no effort setting here, so the model's default applies.

    Returns:
        The launch.
    """
    del reasoning_effort
    env = _base_env(model, gateway)
    env.update(_ENV)
    # agent.ts is a launch file because a protected run's relay rewrite edits
    # it in the workspace; the copy runs after that rewrite, so the build
    # directory gets the relay URL.
    setup = (
        'EVE_DIR="$(mktemp -d)" && mkdir -p "$EVE_DIR/agent" && '
        f'cp {_AGENT_FILE} {EVE_INSTRUCTIONS_FILE} "$EVE_DIR/agent/" && '
        f"printf '%s' {shlex.quote(_package_json())} > \"$EVE_DIR/package.json\" && "
        f"printf '%s' {shlex.quote(EVE_RUNNER)} > \"$EVE_DIR/runner.mjs\""
    )
    return HarnessLaunch(
        instructions_file=EVE_INSTRUCTIONS_FILE,
        install_command=_npm_install("eve", "eve", EVE_VERSION),
        files={_AGENT_FILE: _agent_source(model, gateway)},
        run_command=(
            f"{setup} && "
            f'(cd "$EVE_DIR" && npm install {_NPM_FLAGS} && node_modules/.bin/eve build) >&2 && '
            'EVE_PROJECT_DIR="$EVE_DIR" node "$EVE_DIR/runner.mjs"'
        ),
        env=env,
        parse_output=parse_pi_output,
    )


SPEC = register(
    HarnessSpec(
        id=BLACKBOX_HARNESS_EVE,
        launch=_eve_launch,
        binary="eve",
        version=EVE_VERSION,
        output_format="pi",
        protected_env=_PROTECTED_ENV,
        position=66,
    )
)
