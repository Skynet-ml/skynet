import { spawn, type ChildProcess } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import type { CliId, CliLimit, CliStatus, DockerStatus } from "./cli-detect";

export const STOP_GRACE_MS = 15_000;

// ---------------------------------------------------------------------------
// Onboarding state machine (pure: tested in test/onboarding-state.test.ts)
// ---------------------------------------------------------------------------

export type OnboardingKind =
  | "checking"
  | "docker_missing"
  | "docker_stopped"
  | "no_cli"
  | "cli_too_old"
  | "not_logged_in"
  | "rate_capped"
  | "starting"
  | "sign_in"
  | "ready"
  | "error";

/** Where the hosted-account sign-in is, while kind is "sign_in". */
export type SignInPhase = "waiting" | "unavailable" | "unreachable" | "failed" | "declined";

export interface OnboardingState {
  kind: OnboardingKind;
  clis: CliStatus[];
  docker: DockerStatus | null;
  /** Earliest reset time (ISO UTC) when every usable CLI is capped. */
  resetAt?: string;
  limitMessage?: string;
  /** Short progress step while starting, e.g. "postgres". */
  step?: string;
  error?: string;
  url?: string;
  signIn?: SignInPhase;
}

export interface Inputs {
  docker: DockerStatus;
  clis: CliStatus[];
  limits: Partial<Record<CliId, CliLimit>>;
  now: Date;
  /** The user chose "Continue anyway" on the rate-cap screen. */
  acceptedRateCap: boolean;
}

function isCapped(limit: CliLimit | undefined, now: Date): boolean {
  if (!limit) return false;
  const reset = Date.parse(limit.reset_at);
  return Number.isFinite(reset) && reset > now.getTime();
}

/**
 * Decide which onboarding screen to show. Checks run in dependency order:
 * Docker first (Postgres and every sandbox need it), then a CLI that is
 * installed, new enough, signed in, and not rate capped. A CLI whose login
 * check gave no readable answer (loggedIn null) counts as signed out, so the
 * app never starts on a CLI that cannot run jobs.
 */
export function computeOnboardingState(input: Inputs): OnboardingState {
  const { docker, clis, limits, now } = input;
  const base = { clis, docker };
  if (!docker.path) return { ...base, kind: "docker_missing" };
  if (!docker.running) return { ...base, kind: "docker_stopped" };
  const installed = clis.filter((c) => c.path);
  if (installed.length === 0) return { ...base, kind: "no_cli" };
  const current = installed.filter((c) => c.versionOk);
  if (current.length === 0) return { ...base, kind: "cli_too_old" };
  const signedIn = current.filter((c) => c.loggedIn === true);
  if (signedIn.length === 0) return { ...base, kind: "not_logged_in" };
  const free = signedIn.filter((c) => !isCapped(limits[c.id], now));
  if (free.length === 0 && !input.acceptedRateCap) {
    const capped = signedIn
      .map((c) => limits[c.id] as CliLimit)
      .sort((a, b) => Date.parse(a.reset_at) - Date.parse(b.reset_at));
    return { ...base, kind: "rate_capped", resetAt: capped[0].reset_at, limitMessage: capped[0].message };
  }
  return { ...base, kind: "starting" };
}

// ---------------------------------------------------------------------------
// Child processes
// ---------------------------------------------------------------------------

export interface RunOutput {
  code: number;
  stdout: string;
  stderr: string;
}

const activeCommands = new Set<ChildProcess>();

/** Kill every command started by runCommand that is still running (quit during first-launch setup). */
export function killActiveCommands(): void {
  for (const child of activeCommands) killGroup(child);
  activeCommands.clear();
}

function killGroup(child: ChildProcess): void {
  // pid is undefined when spawn failed; process.kill(-0) would signal our own group.
  if (!child.pid) return;
  try {
    process.kill(-child.pid, "SIGKILL");
  } catch {
    child.kill("SIGKILL");
  }
}

const OUTPUT_CAP = 256 * 1024;

// Long builds stream megabytes; callers only need the end for error messages.
function tail(text: string): string {
  return text.length > OUTPUT_CAP ? text.slice(-OUTPUT_CAP) : text;
}

/** Run a command to completion and capture its output. Never throws. */
export function runCommand(
  file: string,
  args: string[],
  opts: { env: Record<string, string>; cwd?: string; timeoutMs?: number; log?: fs.WriteStream },
): Promise<RunOutput> {
  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let child: ChildProcess;
    try {
      child = spawn(file, args, { env: opts.env, cwd: opts.cwd, detached: true, stdio: ["ignore", "pipe", "pipe"] });
    } catch (err) {
      resolve({ code: 127, stdout: "", stderr: String(err) });
      return;
    }
    activeCommands.add(child);
    const timer = opts.timeoutMs
      ? setTimeout(() => {
          stderr += `\n[timeout after ${opts.timeoutMs} ms]`;
          killGroup(child);
        }, opts.timeoutMs)
      : null;
    child.stdout?.on("data", (d: Buffer) => {
      stdout = tail(stdout + d.toString());
      opts.log?.write(d);
    });
    child.stderr?.on("data", (d: Buffer) => {
      stderr = tail(stderr + d.toString());
      opts.log?.write(d);
    });
    child.on("error", (err) => {
      stderr += String(err);
    });
    child.on("close", (code) => {
      activeCommands.delete(child);
      if (timer) clearTimeout(timer);
      resolve({ code: code ?? 1, stdout, stderr });
    });
  });
}

/**
 * A long-running service (API or frontend). It runs in its own process group
 * so that stopping it also stops grandchildren: `uv run` starts Python as a
 * child, and SIGTERM to uv alone would orphan the server.
 */
export class ManagedProcess {
  private child: ChildProcess | null = null;
  private exitPromise: Promise<void> | null = null;
  exited = false;
  exitCode: number | null = null;

  constructor(
    readonly name: string,
    private readonly file: string,
    private readonly args: string[],
    private readonly opts: { env: Record<string, string>; cwd: string; logFile: string },
  ) {}

  start(): void {
    const log = fs.openSync(this.opts.logFile, "a");
    this.child = spawn(this.file, this.args, {
      env: this.opts.env,
      cwd: this.opts.cwd,
      detached: true,
      stdio: ["ignore", log, log],
    });
    fs.closeSync(log);
    this.exitPromise = new Promise((resolve) => {
      this.child!.on("exit", (code) => {
        this.exited = true;
        this.exitCode = code;
        // The leader can exit before its children. Sweep the group now: later the
        // pid may belong to an unrelated process.
        this.signal("SIGKILL");
        resolve();
      });
      this.child!.on("error", () => {
        this.exited = true;
        resolve();
      });
    });
  }

  get pid(): number | undefined {
    return this.child?.pid;
  }

  private signal(sig: NodeJS.Signals): void {
    const pid = this.child?.pid;
    if (!pid) return;
    try {
      process.kill(-pid, sig);
    } catch {
      // After the leader exits, its bare pid is free for reuse.
      if (this.exited) return;
      try {
        process.kill(pid, sig);
      } catch {
        // Already gone.
      }
    }
  }

  /** SIGTERM the process group, then SIGKILL after the grace period. */
  async stop(graceMs = STOP_GRACE_MS): Promise<void> {
    if (!this.child || !this.exitPromise || this.exited) return;
    this.signal("SIGTERM");
    const timedOut = await Promise.race([
      this.exitPromise.then(() => false),
      new Promise<boolean>((r) => setTimeout(() => r(true), graceMs)),
    ]);
    if (timedOut) this.signal("SIGKILL");
    await this.exitPromise;
  }
}

/** GET a URL and return its status code, or 0 when the request fails. */
export function httpStatus(url: string, timeoutMs = 3_000): Promise<number> {
  return new Promise((resolve) => {
    const req = http.get(url, { timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(res.statusCode ?? 0);
    });
    req.on("timeout", () => req.destroy());
    req.on("error", () => resolve(0));
  });
}

/** Poll until `check` returns true. Fails early when `alive` returns false. */
export async function waitFor(
  what: string,
  check: () => Promise<boolean>,
  opts: { timeoutMs: number; intervalMs?: number; alive?: () => boolean },
): Promise<void> {
  const deadline = Date.now() + opts.timeoutMs;
  while (Date.now() < deadline) {
    if (await check()) return;
    if (opts.alive && !opts.alive()) throw new Error(`${what} exited before it was ready`);
    await new Promise((r) => setTimeout(r, opts.intervalMs ?? 500));
  }
  throw new Error(`${what} was not ready after ${Math.round(opts.timeoutMs / 1000)} s`);
}
