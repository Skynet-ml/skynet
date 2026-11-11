"""eve harness (npm ``eve``), a filesystem-first agent framework run through its local HTTP session API.

Adapted from Harbor 0.25.0 (Apache-2.0), agents/installed/eve.py and
agents/installed/eve_runner.mjs. Skynet generates the eve project itself
instead of copying a user project, and the runner prints Pi-shaped JSON lines
so the bridge's Pi parser reads the answer, the usage and the tool calls.
"""

from __future__ import annotations

import json
import shlex

from .....exceptions import ServiceError
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
# The sandbox image preinstalls eve and its peers here from
# sandbox-runtime/package-lock.json, so a protected run with no registry route
# can still build the project.
EVE_IMAGE_DEPS = "/usr/local/lib/skynet-harnesses/node_modules"
# eve needs Node >=24, but the other harness CLIs run on the image's Node 22,
# so the image keeps a second Node binary for eve alone.
_IMAGE_NODE24_BIN = "/opt/node24/bin"
# eve 1.0.0-rc.6's ``AgentReasoningDefinition`` levels. Skynet passes them
# unchanged and refuses any other level, so a run never gets an effort the
# user did not pick. ``provider-default`` is left out: no effort means that.
EVE_REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh")
_NPM_FLAGS = "--no-fund --no-audit --loglevel=error --progress=false"
_ENV = {
    "EVE_TELEMETRY_DISABLED": "1",
    "NPM_CONFIG_UPDATE_NOTIFIER": "false",
}
# The direct provider credentials that eve's AI SDK providers read. A
# protected run blanks them, like goose, so no tool command or eve process can
# reach a model outside the Skynet gateway; the runner also drops them.
_PROVIDER_KEYS = (
    "AI_GATEWAY_API_KEY",
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GENERATIVE_AI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "VERCEL_OIDC_TOKEN",
    "XAI_API_KEY",
)
# A protected sandbox has no route to the internet, so the project install
# must come from the npm cache and fail fast instead of waiting on the registry.
_PROTECTED_ENV = {
    **dict.fromkeys(_PROVIDER_KEYS, ""),
    "EVE_TELEMETRY_DISABLED": "1",
    "NPM_CONFIG_UPDATE_NOTIFIER": "false",
    "NPM_CONFIG_OFFLINE": "true",
}

# The runner starts ``eve start``, sends the case prompt as one session turn
# and prints Pi's ``tool_execution_start`` and ``message_end`` events as the
# session streams them, so the bridge's tool-call and cost caps can stop a
# running session. eve 1.0.0-rc.6 streams protocol v27, which sends one
# ``call.requested`` per tool call and the step usage in
# ``usage.recorded``'s ``data.usage``. The runner drops every direct provider
# credential from the server env, so the AI SDK can only reach the model
# through the Skynet gateway that agent.ts names. ``eve start`` rejects every
# caller without a channel auth, so the runner makes a per-run bearer token,
# gives it to the server, and signs its own session with it. The server
# listens on a free port that the OS picks, and the health route has no auth,
# so the runner also checks that the server on that port accepts its token
# before it sends the prompt.
EVE_RUNNER = r"""import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { createServer } from "node:net";
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
const OK_STATUSES = new Set(["completed"]);
const projectDir = process.env.EVE_PROJECT_DIR;
const host = "127.0.0.1";
const port = await freePort();
const serverUrl = `http://${host}:${port}`;
const startupTimeoutMs = 60000;
const runnerToken = randomBytes(32).toString("hex");

const SETTLED_TYPES = new Set(["turn.settled", "session.ended"]);
const FAILED_OUTCOMES = new Set(["failed", "cancelled"]);
let failure = null;

let server;
try {
  server = startServer();
  await waitForServer(server);
  const prompt = await readFile(process.env.APP_PROMPT_FILE, "utf8");
  const require = createRequire(join(projectDir, "package.json"));
  const { Client } = await import(pathToFileURL(require.resolve("eve/client")).href);
  const client = new Client({ baseUrl: serverUrl, auth: { bearer: runnerToken } });
  const { delivery } = await client.sessions.create({ message: prompt });
  for await (const event of delivery.stream()) handleEvent(event);
  const result = await delivery.result();
  printMessage(messageText(result.reply), { input: 0, output: 0 });
  if (failure === null && !OK_STATUSES.has(result.status)) failure = statusFailure(result);
  if (failure !== null) throw new Error(failure);
} finally {
  if (server) await stopServer(server);
}

function handleEvent(event) {
  const type = event?.type;
  const data = event?.data ?? {};
  if (type === "call.requested") {
    console.log(JSON.stringify({ type: "tool_execution_start", toolCallId: String(data.callId ?? "") }));
  } else if (type === "usage.recorded") {
    const usage = data.usage ?? {};
    printMessage("", { input: tokenCount(usage.inputTokens), output: tokenCount(usage.outputTokens) });
  } else if (SETTLED_TYPES.has(type) && FAILED_OUTCOMES.has(data.outcome)) {
    failure ??= withDetail(`${type} ended ${data.outcome} during the eve run`, data.error?.message);
  }
}

function statusFailure(result) {
  const status = result.status ?? "unknown";
  const detail = status === "awaiting-input"
    ? "the agent asked for input that this run cannot give"
    : result.error?.message;
  return withDetail(`eve run ended with status ${status}`, detail);
}

// eve can send an empty error message, so the status always names the failure.
function withDetail(text, detail) {
  return detail ? `${text}: ${detail}` : text;
}

function printMessage(text, usage) {
  console.log(JSON.stringify({ type: "message_end", message: { role: "assistant", content: [{ type: "text", text }], usage } }));
}

function startServer() {
  const env = { ...process.env };
  for (const key of PROVIDER_ENV_KEYS) delete env[key];
  env.EVE_RUNNER_TOKEN = runnerToken;
  const child = spawn(join(projectDir, "node_modules", ".bin", "eve"), ["start", "--host", host, "--port", String(port)], {
    cwd: projectDir,
    env,
    stdio: ["ignore", "pipe", "inherit"],
  });
  // stdout is the event stream the bridge parses, so server logs go to stderr.
  child.stdout?.pipe(process.stderr);
  return child;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, host, () => {
      const { port: picked } = probe.address();
      probe.close(() => resolve(picked));
    });
  });
}

async function waitForServer(child) {
  const deadline = Date.now() + startupTimeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`eve start exited before it was ready (code ${child.exitCode})`);
    let health;
    try {
      health = await fetch(`${serverUrl}/eve/v1/health`);
    } catch (error) {
      lastError = error;
    }
    if (health?.ok) {
      // Another process can take the port between freePort and eve start;
      // only this run's server accepts the runner token.
      const info = await fetch(`${serverUrl}/eve/v1/info`, { headers: { authorization: `Bearer ${runnerToken}` } });
      if (info.ok) return;
      throw new Error(`the server on port ${port} refused the runner token (${info.status})`);
    }
    if (health) lastError = new Error(`health returned ${health.status}`);
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`timed out waiting for eve start: ${lastError?.message ?? "unknown"}`);
}

function messageText(message) {
  if (typeof message === "string") return message;
  if (Array.isArray(message)) return message.filter((part) => part?.type === "text").map((part) => part.text ?? "").join("\n");
  if (message && typeof message === "object") return messageText(message.text ?? message.content ?? "");
  return "";
}

function tokenCount(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
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

# eve's default sandbox has no provider in a production ``eve build`` without
# Docker or Vercel, so its tools would fail on first use. This provider runs
# them as host processes of the Skynet sandbox box, and that box is the
# isolation boundary. The provider does not keep paths inside
# ``EVE_WORKSPACE``; it only starts there. When the run has hidden paths, the
# bridge sets ``APP_GUARD_EVE``, and every tool call asks the shared guard
# check first, so a call that names a hidden path fails. The eve server must
# keep APP_API_KEY (agent.ts reads it) and EVE_RUNNER_TOKEN (the channel reads
# it), so the provider removes them, with the project paths, only from the env
# of the commands it starts. ``with_legacy_env`` also copies APP_API_KEY to
# SKYNET_API_KEY, so the provider removes that legacy twin too. The box
# process can also hold the tool relay, package relay, candidate and host
# agent tokens, the task and metric keys and the ShinkaEvolve route keys, so
# the provider removes them and their SKYNET_ twins. The native runtime also
# puts the gateway key in ANTHROPIC_AUTH_TOKEN, so the provider removes it too. eve's
# contract says ``bash -lc``, but a login shell reads /etc/profile, which
# resets PATH and drops the Node 24 directory that the launch adds. The
# image's ENV PATH already holds the venv, so ``bash -c`` keeps the server
# PATH and every image tool. Each command starts detached, in its own session
# and process group, so an abort or ``kill()`` stops the whole group. The
# bridge's stop sweep finds these groups through the process tree; on Linux
# the bridge starts the harness shell under a reaper process that is a child
# subreaper, so a background job that a finished command left behind stays in
# that tree.
EVE_SANDBOX = r"""import { spawn } from "node:child_process";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, isAbsolute } from "node:path";
import { Readable, Writable } from "node:stream";
import { pathToFileURL } from "node:url";
import { defineSandbox } from "eve/sandbox";
import { defineSandboxProvider, SANDBOX_AGENT_ENV } from "eve/sandbox/provider";

const DROPPED_ENV = [
  "APP_API_KEY",
  "SKYNET_API_KEY",
  "APP_CANDIDATE_TOKEN",
  "SKYNET_CANDIDATE_TOKEN",
  "APP_HOST_AGENT_TOKEN",
  "SKYNET_HOST_AGENT_TOKEN",
  "APP_PACKAGE_RELAY_TOKEN",
  "SKYNET_PACKAGE_RELAY_TOKEN",
  "APP_TOOL_RELAY_TOKEN",
  "SKYNET_TOOL_RELAY_TOKEN",
  "APP_TASK_API_KEY",
  "SKYNET_TASK_API_KEY",
  "APP_METRIC_API_KEY",
  "SKYNET_METRIC_API_KEY",
  "ANTHROPIC_AUTH_TOKEN",
  "EVE_PROJECT_DIR",
  "EVE_RUNNER_TOKEN",
  "EVE_WORKSPACE",
];
// APP_SHINKA_KEY_<n>, APP_SHINKA_META_KEY, APP_SHINKA_NOVELTY_KEY and
// APP_SHINKA_EMBED_KEY, and their SKYNET_ twins.
const DROPPED_ENV_PATTERN = /^(APP|SKYNET)_SHINKA_\w*KEY/;
let guardModule;

async function guard(tool, input) {
  const file = process.env.APP_GUARD_EVE;
  if (!file) return;
  let reason;
  try {
    guardModule ??= import(pathToFileURL(file).href);
    reason = (await guardModule).appGuardReason(tool, input);
  } catch {
    reason = "the Skynet guard could not check this call";
  }
  if (reason) throw new Error(reason);
}

// Each command leads its own process group, so a kill also stops the
// processes it started; killing only bash would leave them running.
function killProcessGroup(pid) {
  if (pid === undefined) return;
  try {
    process.kill(-pid, "SIGKILL");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
  }
}

function workspaceRoot() {
  const root = process.env.EVE_WORKSPACE;
  if (!root || !isAbsolute(root)) throw new Error("EVE_WORKSPACE must name the absolute run workspace");
  return root;
}

function connect(root) {
  const base = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value !== undefined && !DROPPED_ENV.includes(key) && !DROPPED_ENV_PATTERN.test(key)) base[key] = value;
  }
  return {
    workspaceRoot: root,
    async spawn({ signal, stdin, command, env, workingDirectory }) {
      await guard("bash", { command, cwd: workingDirectory });
      const child = spawn("bash", ["-c", command], {
        cwd: workingDirectory,
        detached: true,
        env: { ...base, ...SANDBOX_AGENT_ENV, ...env },
        stdio: [stdin ? "pipe" : "ignore", "pipe", "pipe"],
      });
      const killGroup = () => killProcessGroup(child.pid);
      if (signal?.aborted) killGroup();
      else signal?.addEventListener("abort", killGroup, { once: true });
      const exit = new Promise((resolve, reject) => {
        child.once("error", (error) => (child.pid === undefined ? reject(error) : undefined));
        child.once("close", (code) => {
          signal?.removeEventListener("abort", killGroup);
          resolve({ exitCode: code ?? 1 });
        });
      });
      return {
        pid: child.pid,
        stdin: stdin && child.stdin ? Writable.toWeb(child.stdin) : undefined,
        stdout: Readable.toWeb(child.stdout),
        stderr: Readable.toWeb(child.stderr),
        wait: () => exit,
        kill: async () => killGroup(),
      };
    },
    async readFile({ path }) {
      await guard("read_file", { path });
      try {
        return new Uint8Array(await readFile(path));
      } catch (error) {
        if (error?.code === "ENOENT") return null;
        throw error;
      }
    },
    async writeFiles({ files }) {
      for (const file of files) await guard("write_file", { path: file.path });
      for (const file of files) {
        await mkdir(dirname(file.path), { recursive: true });
        await writeFile(file.path, file.content);
      }
    },
    async removePath({ force, path, recursive }) {
      await guard("remove", { path });
      await rm(path, { force, recursive });
    },
    async stop() {},
  };
}

const WorkspaceSandbox = defineSandboxProvider({ name: "skynet-workspace", environment() {
  return {
    async prepare() {
      return {};
    },
    async start() {
      return { connection: connect(workspaceRoot()), state: {} };
    },
    async resume() {
      return connect(workspaceRoot());
    },
    async onSessionEnd() {},
  };
} });

export const environment = WorkspaceSandbox.environment();
export default defineSandbox(() => environment.open());
"""

# ``eve start`` answers 401 to every caller under the default auth walk, so the
# channel accepts only the bearer token that the runner made for this run.
EVE_CHANNEL = r"""import { timingSafeEqual } from "node:crypto";
import { extractBearerToken } from "eve/channels/auth";
import { eveChannel } from "eve/channels/eve";

const runnerToken = (request) => {
  const expected = process.env.EVE_RUNNER_TOKEN ?? "";
  const given = extractBearerToken(request.headers.get("authorization")) ?? "";
  if (!expected || given.length !== expected.length) return null;
  if (!timingSafeEqual(Buffer.from(given), Buffer.from(expected))) return null;
  return { authenticator: "skynet-runner", principalId: "skynet-runner", principalType: "service" };
};

export default eveChannel({ auth: [runnerToken] });
"""

# eve's default ``agent`` tool starts subagents whose tool calls stream in
# their own sessions, so the runner would not count them against the tool-call
# and cost caps. A file in ``agent/tools/`` that exports ``disableTool()``
# removes that default tool.
EVE_DISABLE_AGENT_TOOL = 'import { disableTool } from "eve/tools";\n\nexport default disableTool();\n'
# The image can lack the Node 24 binary (for example a local run), and eve on
# an older Node fails late with a syntax or engine error, so the run stops
# first with a clear message.
_NODE_CHECK = (
    'node -e \'if (+process.versions.node.split(".")[0] < 24) '
    '{ console.error("eve needs Node 24 or newer; found " + process.version); process.exit(1); }\''
)


def _reasoning_level(reasoning_effort: str | None) -> str | None:
    """Return eve's ``reasoning`` level for the user's effort.

    Args:
        reasoning_effort: The user's effort for the model, or ``None``.

    Returns:
        The level, or ``None`` to keep the model's default.

    Raises:
        ServiceError: When eve has no level with that name.
    """
    if not reasoning_effort:
        return None
    if reasoning_effort not in EVE_REASONING_LEVELS:
        raise ServiceError(
            f"The eve harness does not support the effort '{reasoning_effort}'. "
            f"Pick one of: {', '.join(EVE_REASONING_LEVELS)}."
        )
    return reasoning_effort


def _agent_source(model: str, gateway: GatewayConfig, reasoning: str | None = None) -> str:
    """Return the eve ``agent/agent.ts`` that sends every model call to the Skynet gateway.

    The gateway URL is a literal so a protected run's relay rewrite finds it;
    the key stays in the environment and never lands in a file.

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key.
        reasoning: eve's ``reasoning`` level, or ``None`` for the model's default.

    Returns:
        The TypeScript source.
    """
    # The gateway's model ids are not in the AI Gateway catalog that ``eve
    # build`` reads, so the build fails unless the agent states a context window.
    options = f"model: gateway({json.dumps(model)}), modelContextWindowTokens: 200_000"
    if reasoning:
        options += f", reasoning: {json.dumps(reasoning)}"
    return (
        'import { createOpenAICompatible } from "@ai-sdk/openai-compatible";\n'
        'import { defineAgent } from "eve";\n\n'
        "const gateway = createOpenAICompatible({\n"
        f"  name: {json.dumps(PROVIDER)},\n"
        f"  baseURL: {json.dumps(gateway.url)},\n"
        "  apiKey: process.env.APP_API_KEY,\n"
        "  includeUsage: true,\n"
        "});\n\n"
        f"export default defineAgent({{ {options} }});\n"
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
    project outside the workspace, adds the workspace sandbox and the runner
    channel, then builds it there, since ``eve build`` compiles those
    instructions. So package.json, the runner, node_modules and the build
    output never reach the version patch, and an exit trap removes the project.
    A SIGKILL skips that trap, so the project can stay in the box's temp
    directory until the box ends; ``mktemp -d`` makes it mode 0700.
    The runner then sends the case prompt.

    Args:
        model: Target model id.
        gateway: Gateway URL and key.
        reasoning_effort: The user's effort, passed as eve's ``reasoning``
            level, or ``None`` for the model's default.

    Returns:
        The launch.

    Raises:
        ServiceError: When eve has no level with the effort's name.
    """
    env = _base_env(model, gateway)
    env.update(_ENV)
    # agent.ts is a launch file because a protected run's relay rewrite edits
    # it in the workspace; the copy runs after that rewrite, so the build
    # directory gets the relay URL.
    setup = (
        'export EVE_WORKSPACE="$PWD" && EVE_DIR="$(mktemp -d)" && trap \'rm -rf "$EVE_DIR"\' EXIT && '
        'mkdir -p "$EVE_DIR/agent/channels" "$EVE_DIR/agent/tools" && '
        f'cp {_AGENT_FILE} {EVE_INSTRUCTIONS_FILE} "$EVE_DIR/agent/" && '
        f"printf '%s' {shlex.quote(EVE_SANDBOX)} > \"$EVE_DIR/agent/sandbox.ts\" && "
        f"printf '%s' {shlex.quote(EVE_CHANNEL)} > \"$EVE_DIR/agent/channels/eve.ts\" && "
        f"printf '%s' {shlex.quote(EVE_DISABLE_AGENT_TOOL)} > \"$EVE_DIR/agent/tools/agent.ts\" && "
        f"printf '%s' {shlex.quote(_package_json())} > \"$EVE_DIR/package.json\" && "
        f"printf '%s' {shlex.quote(EVE_RUNNER)} > \"$EVE_DIR/runner.mjs\" && "
        f'if [ -x {_IMAGE_NODE24_BIN}/node ]; then PATH="{_IMAGE_NODE24_BIN}:$PATH"; fi && '
        f"{_NODE_CHECK}"
    )
    # The image's node_modules is read-only and ``eve build`` writes its cache
    # under node_modules/.cache, so the project gets its own node_modules that
    # links each preinstalled package instead of one link to the whole folder.
    deps = (
        f'if [ -d "{EVE_IMAGE_DEPS}/eve" ]; then mkdir node_modules && '
        f'for dep in "{EVE_IMAGE_DEPS}"/* "{EVE_IMAGE_DEPS}"/.bin; do ln -s "$dep" node_modules/ || exit 1; done; '
        f"else npm install {_NPM_FLAGS}; fi"
    )
    return HarnessLaunch(
        instructions_file=EVE_INSTRUCTIONS_FILE,
        install_command=_npm_install("eve", "eve", EVE_VERSION),
        files={_AGENT_FILE: _agent_source(model, gateway, _reasoning_level(reasoning_effort))},
        run_command=(
            f"{setup} && "
            f'(cd "$EVE_DIR" && {deps} && node_modules/.bin/eve build) >&2 && '
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
        supported_efforts=EVE_REASONING_LEVELS,
    )
)
