import { spawn } from "node:child_process";
import os from "node:os";

/**
 * The only variables a child inherits from the app's environment (§1.7). Every
 * other variable is dropped, so no provider key, base URL, profile, federation
 * id, socket or config switch reaches claude, codex or the stack. The
 * stack gets its settings (APP_*, REMOTE_DB_URL, ports, secrets) as explicit
 * overrides from index.ts, never from the user's shell.
 * CLAUDE_CODE_OAUTH_TOKEN stays: it is the subscription login itself.
 * The DOCKER_* keys select the Docker daemon (Colima, remote contexts); they
 * carry no provider credential.
 */
export const ALLOWED_ENV_KEYS = [
  "PATH",
  "HOME",
  "USER",
  "LOGNAME",
  "SHELL",
  "TMPDIR",
  "LANG",
  "TERM",
  "TZ",
  "CLAUDE_CODE_OAUTH_TOKEN",
  "CLAUDE_CONFIG_DIR",
  "CODEX_HOME",
  "HTTP_PROXY",
  "HTTPS_PROXY",
  "ALL_PROXY",
  "NO_PROXY",
  "http_proxy",
  "https_proxy",
  "all_proxy",
  "no_proxy",
  "SSL_CERT_FILE",
  "SSL_CERT_DIR",
  "NODE_EXTRA_CA_CERTS",
  "CODEX_CA_CERTIFICATE",
  "DOCKER_HOST",
  "DOCKER_CONTEXT",
  "DOCKER_CONFIG",
  "DOCKER_CERT_PATH",
  "DOCKER_TLS_VERIFY",
] as const;

/** Every variable with one of these prefixes is inherited too (locale and XDG directories). */
export const ALLOWED_ENV_PREFIXES = ["LC_", "XDG_"] as const;

/** Whether a child may inherit this variable from the app's environment. */
export function isAllowedEnvKey(key: string): boolean {
  return (ALLOWED_ENV_KEYS as readonly string[]).includes(key) || ALLOWED_ENV_PREFIXES.some((prefix) => key.startsWith(prefix));
}

export const LOGIN_SHELL_TIMEOUT_MS = 10_000;

/** The PATH used when the login shell cannot be read. */
export function fallbackPath(home: string = os.homedir()): string {
  return ["/opt/homebrew/bin", "/usr/local/bin", `${home}/.local/bin`, "/usr/bin", "/bin"].join(":");
}

/** Parse the output of `env -0` (NUL-separated KEY=VALUE records). */
export function parseEnvNul(output: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const record of output.split("\0")) {
    const eq = record.indexOf("=");
    if (eq <= 0) continue;
    // A login shell can print a banner before `env -0` runs; only the text
    // after the last newline of the first record belongs to the variable name.
    const rawKey = record.slice(0, eq);
    const key = rawKey.slice(rawKey.lastIndexOf("\n") + 1);
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    env[key] = record.slice(eq + 1);
  }
  return env;
}

export type ExecFileLike = (
  file: string,
  args: string[],
  options: { timeout: number; env: NodeJS.ProcessEnv },
) => Promise<{ stdout: string }>;

const defaultExec: ExecFileLike = (file, args, options) =>
  new Promise((resolve, reject) => {
    // Its own process group keeps an interactive shell off any terminal, and lets
    // the timeout kill what the rc files started. It ignores SIGTERM, so SIGKILL;
    // stdin is /dev/null, so an rc file that reads it gets EOF.
    const child = spawn(file, args, { env: options.env, detached: true, stdio: ["ignore", "pipe", "ignore"] });
    let stdout = "";
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    const killGroup = () => {
      try {
        process.kill(-child.pid!, "SIGKILL");
      } catch {
        // Already gone.
      }
    };
    const timer = setTimeout(() => {
      killGroup();
      child.stdout.destroy();
      reject(new Error("login shell timed out"));
    }, options.timeout);
    child.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });
    const settle = (code: number | null) => {
      clearTimeout(timer);
      if (code === 0) resolve({ stdout });
      else reject(new Error(`login shell exited with ${code}`));
    };
    // A job the rc files put in the background can hold stdout open after the
    // shell exits, so "close" can come late. The output is in after a short grace.
    // With the shell gone, the group holds only those jobs; each check would
    // otherwise leave one more copy running after the app quits. They get the
    // shell's own time budget first, so rc upkeep (zcompile under a lock) ends cleanly.
    child.on("exit", (code) => {
      setTimeout(killGroup, options.timeout).unref();
      if (code !== 0) return;
      setTimeout(() => {
        child.stdout.destroy();
        settle(code);
      }, 100);
    });
    child.on("close", settle);
  });

/**
 * Read PATH from the user's login shell. Apps opened from Finder inherit a
 * minimal launchd PATH that misses Homebrew, nvm and ~/.local/bin, which is
 * where docker, codex and claude usually live.
 */
export async function loginShellPath(
  opts: { shell?: string; home?: string; exec?: ExecFileLike; timeoutMs?: number } = {},
): Promise<{ path: string; fromShell: boolean }> {
  const shell = opts.shell || process.env.SHELL || "/bin/zsh";
  const home = opts.home || os.homedir();
  const exec = opts.exec ?? defaultExec;
  // Interactive first: zsh reads .zshrc only in interactive shells, and nvm's
  // installer puts its PATH lines there. A .zshrc that execs tmux or another
  // shell prints no env, so a plain login shell is the next try.
  for (const flags of ["-ilc", "-lc"]) {
    try {
      const { stdout } = await exec(shell, [flags, "env -0"], {
        timeout: opts.timeoutMs ?? LOGIN_SHELL_TIMEOUT_MS,
        env: { ...process.env, HOME: home, SHELL: shell, TERM: "dumb" },
      });
      const parsed = parseEnvNul(stdout).PATH;
      if (parsed && parsed.trim()) return { path: parsed, fromShell: true };
    } catch {
      // Timeout, missing shell or a broken rc file: the fallback PATH still finds Homebrew installs.
    }
  }
  return { path: fallbackPath(home), fromShell: false };
}

/**
 * Build a child process environment: only the allowed variables of the base
 * env, plus explicit overrides. CLI checks pass only PATH; the stack passes its
 * own settings. ELECTRON_RUN_AS_NODE is not allowed, so a stray inherited copy
 * (it turns every spawned Electron binary into plain Node) is dropped, while an
 * explicit override still sets it for the bundled web server.
 */
export function buildChildEnv(
  base: NodeJS.ProcessEnv,
  overrides: Record<string, string | undefined> = {},
): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(base)) {
    if (value !== undefined && isAllowedEnvKey(key)) env[key] = value;
  }
  for (const [key, value] of Object.entries(overrides)) {
    if (value === undefined) delete env[key];
    else env[key] = value;
  }
  return env;
}

/** Name a dotenv file that the backend or `next dev` loads: `.env` or `.env.*`, except `.env.example`. */
export function isLoadedEnvFile(name: string): boolean {
  return name === ".env" || (name.startsWith(".env.") && name !== ".env.example");
}
