import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { ALLOWED_ENV_KEYS, ALLOWED_ENV_PREFIXES, buildChildEnv, fallbackPath, isLoadedEnvFile, loginShellPath, parseEnvNul } from "../src/main/shell-env";

describe("parseEnvNul", () => {
  it("parses NUL-separated records, including values with = and newlines", () => {
    const env = parseEnvNul("PATH=/opt/homebrew/bin:/usr/bin\0EMPTY=\0MULTI=a\nb=c\0");
    expect(env).toEqual({ PATH: "/opt/homebrew/bin:/usr/bin", EMPTY: "", MULTI: "a\nb=c" });
  });

  it("ignores a banner printed by the login shell before env -0", () => {
    const env = parseEnvNul("Welcome to zsh!\nlast login: today\nPATH=/usr/local/bin:/bin\0HOME=/Users/x\0");
    expect(env.PATH).toBe("/usr/local/bin:/bin");
    expect(env.HOME).toBe("/Users/x");
  });

  it("skips records without a valid name", () => {
    expect(parseEnvNul("=bad\0no-equals\0 1X=y\0OK=1")).toEqual({ OK: "1" });
  });
});

describe("loginShellPath", () => {
  it("returns the login shell PATH", async () => {
    const result = await loginShellPath({
      shell: "/bin/zsh",
      home: "/Users/x",
      exec: async (file, args) => {
        expect(file).toBe("/bin/zsh");
        expect(args).toEqual(["-ilc", "env -0"]);
        return { stdout: "HOME=/Users/x\0PATH=/Users/x/.nvm/bin:/opt/homebrew/bin:/usr/bin\0" };
      },
    });
    expect(result).toEqual({ path: "/Users/x/.nvm/bin:/opt/homebrew/bin:/usr/bin", fromShell: true });
  });

  it("falls back when the shell fails or times out", async () => {
    const result = await loginShellPath({
      shell: "/bin/zsh",
      home: "/Users/x",
      exec: async () => {
        throw new Error("timed out");
      },
    });
    expect(result).toEqual({ path: fallbackPath("/Users/x"), fromShell: false });
    expect(result.path).toBe("/opt/homebrew/bin:/usr/local/bin:/Users/x/.local/bin:/usr/bin:/bin");
  });

  it("falls back when the output has no PATH", async () => {
    const result = await loginShellPath({ home: "/Users/x", exec: async () => ({ stdout: "HOME=/Users/x\0" }) });
    expect(result.fromShell).toBe(false);
  });

  describe("with a real zsh", () => {
    const zdotdir = process.env.ZDOTDIR;
    beforeEach(() => delete process.env.ZDOTDIR);
    afterEach(() => {
      if (zdotdir !== undefined) process.env.ZDOTDIR = zdotdir;
    });

    function homeWithZshrc(zshrc: string, zprofile = ""): string {
      const home = fs.mkdtempSync(path.join(os.tmpdir(), "skynet-shell-env-"));
      fs.writeFileSync(path.join(home, ".zshrc"), zshrc);
      fs.writeFileSync(path.join(home, ".zprofile"), zprofile);
      return home;
    }

    function alive(pid: number): boolean {
      try {
        process.kill(pid, 0);
        return true;
      } catch {
        return false;
      }
    }

    it("reads .zshrc, where nvm puts its PATH, even when it reads stdin", async () => {
      const home = homeWithZshrc("read x\nexport PATH=/from/zshrc:$PATH\n");
      const result = await loginShellPath({ shell: "/bin/zsh", home, timeoutMs: 5_000 });
      expect(result.fromShell).toBe(true);
      expect(result.path.split(":")).toContain("/from/zshrc");
    });

    it("keeps the output when .zshrc leaves a background job on stdout", async () => {
      const home = homeWithZshrc("export PATH=/from/zshrc:$PATH\nsleep 5 &\n");
      const started = Date.now();
      const result = await loginShellPath({ shell: "/bin/zsh", home, timeoutMs: 3_000 });
      expect(result.path.split(":")).toContain("/from/zshrc");
      expect(Date.now() - started).toBeLessThan(2_000);
    });

    it("reads a plain login shell when .zshrc outlasts the timeout", async () => {
      const home = homeWithZshrc("sleep 30\n", "export PATH=/from/zprofile:$PATH\n");
      const result = await loginShellPath({ shell: "/bin/zsh", home, timeoutMs: 500 });
      expect(result.fromShell).toBe(true);
      expect(result.path.split(":")).toContain("/from/zprofile");
    });

    it("falls back when the login files outlast the timeout", async () => {
      const home = homeWithZshrc("", "sleep 30\n");
      const started = Date.now();
      const result = await loginShellPath({ shell: "/bin/zsh", home, timeoutMs: 500 });
      expect(result.fromShell).toBe(false);
      expect(Date.now() - started).toBeLessThan(5_000);
    });

    it("stops what .zshrc started when the timeout kills the shell", async () => {
      const home = homeWithZshrc('sleep 30 &\necho $! > "$HOME/child"\nsleep 30\n');
      await loginShellPath({ shell: "/bin/zsh", home, timeoutMs: 500 });
      const child = Number(fs.readFileSync(path.join(home, "child"), "utf8").trim());
      await expect.poll(() => alive(child), { timeout: 2_000 }).toBe(false);
    });

    it("stops what .zshrc started in the background once the shell's time is up", async () => {
      for (const job of ["sleep 30 &", "sleep 30 >/dev/null 2>&1 &"]) {
        const home = homeWithZshrc(`export PATH=/from/zshrc:$PATH\n${job}\necho $! > "$HOME/child"\n`);
        const result = await loginShellPath({ shell: "/bin/zsh", home, timeoutMs: 500 });
        expect(result.path.split(":")).toContain("/from/zshrc");
        const child = Number(fs.readFileSync(path.join(home, "child"), "utf8").trim());
        await expect.poll(() => alive(child), { timeout: 3_000 }).toBe(false);
      }
    });

    it("lets short .zshrc upkeep in the background finish", async () => {
      const home = homeWithZshrc(
        'export PATH=/from/zshrc:$PATH\n{ mkdir "$HOME/zwc.lock"; sleep 0.3; rmdir "$HOME/zwc.lock" } &!\n',
      );
      const result = await loginShellPath({ shell: "/bin/zsh", home, timeoutMs: 3_000 });
      expect(result.path.split(":")).toContain("/from/zshrc");
      await expect.poll(() => fs.existsSync(path.join(home, "zwc.lock")), { timeout: 2_000 }).toBe(false);
    });

    it("runs the shell in its own process group, so it never takes over a terminal", async () => {
      const home = homeWithZshrc('echo "$$ $(ps -o pgid= -p $$)" > "$HOME/pgid"\n');
      await loginShellPath({ shell: "/bin/zsh", home, timeoutMs: 5_000 });
      const [pid, pgid] = fs.readFileSync(path.join(home, "pgid"), "utf8").trim().split(/\s+/);
      expect(pgid).toBe(pid);
    });

    it("falls back to a login shell when .zshrc replaces the shell", async () => {
      const home = homeWithZshrc("exec /bin/sh -c true\n", "export PATH=/from/zprofile:$PATH\n");
      const result = await loginShellPath({ shell: "/bin/zsh", home, timeoutMs: 5_000 });
      expect(result.fromShell).toBe(true);
      expect(result.path.split(":")).toContain("/from/zprofile");
    });
  });
});

// Every key named in the deny lists of rounds 1-3 and in the SKY-112 review,
// plus variables that change how Node, Python or uv behave.
const DROPPED_KEYS = [
  "OPENROUTER_API_KEY",
  "ANTHROPIC_API_KEY",
  "ANTHROPIC_AUTH_TOKEN",
  "ANTHROPIC_BASE_URL",
  "ANTHROPIC_PROFILE",
  "ANTHROPIC_FEDERATION_RULE_ID",
  "ANTHROPIC_ORGANIZATION_ID",
  "ANTHROPIC_UNIX_SOCKET",
  "AWS_BEARER_TOKEN_BEDROCK",
  "OPENAI_API_KEY",
  "OPENAI_BASE_URL",
  "OPENAI_FEDERATION_RULE_ID",
  "OPENAI_IDENTITY_TOKEN_FILE",
  "CODEX_API_KEY",
  "CODEX_ACCESS_TOKEN",
  "FACTORY_API_KEY",
  "FACTORY_API_BASE_URL",
  "FACTORY_API_BASE_URL_EU",
  "FACTORY_HOME_OVERRIDE",
  "FACTORY_EXTRA_CA_CERTS",
  "CLAUDE_CODE_USE_BEDROCK",
  "CLAUDE_CODE_USE_VERTEX",
  "CLAUDE_CODE_USE_FOUNDRY",
  "VERCEL_TOKEN",
  "STRIPE_SECRET_KEY",
  "STRIPE_WEBHOOK_SECRET",
  "RAILWAY_ENVIRONMENT_NAME",
  "NODE_OPTIONS",
  "PYTHONPATH",
  "UV_INDEX_URL",
  "APP_REMOTE_MODE",
  "DATABASE_URL",
  "ELECTRON_RUN_AS_NODE",
  "SOME_JUNK",
];

const KEPT: Record<string, string> = {
  PATH: "/opt/homebrew/bin:/bin",
  HOME: "/Users/x",
  USER: "x",
  LOGNAME: "x",
  SHELL: "/bin/zsh",
  TMPDIR: "/var/folders/t/",
  LANG: "he_IL.UTF-8",
  TERM: "xterm",
  TZ: "Asia/Jerusalem",
  CLAUDE_CODE_OAUTH_TOKEN: "subscription",
  CLAUDE_CONFIG_DIR: "/Users/x/.claude",
  CODEX_HOME: "/Users/x/.codex",
  HTTP_PROXY: "http://proxy:1",
  HTTPS_PROXY: "http://proxy:1",
  ALL_PROXY: "socks5://proxy:2",
  NO_PROXY: "localhost",
  http_proxy: "http://proxy:1",
  https_proxy: "http://proxy:1",
  all_proxy: "socks5://proxy:2",
  no_proxy: "localhost",
  SSL_CERT_FILE: "/etc/cert.pem",
  SSL_CERT_DIR: "/etc/certs",
  NODE_EXTRA_CA_CERTS: "/etc/ca.pem",
  CODEX_CA_CERTIFICATE: "/etc/ca.pem",
  DOCKER_HOST: "unix:///Users/x/.colima/docker.sock",
  LC_ALL: "C",
  LC_CTYPE: "UTF-8",
  XDG_CONFIG_HOME: "/Users/x/.config",
  XDG_CACHE_HOME: "/Users/x/.cache",
};

describe("buildChildEnv", () => {
  it("keeps only the allowed variables", () => {
    const base: NodeJS.ProcessEnv = { ...KEPT };
    for (const key of DROPPED_KEYS) base[key] = "secret";
    expect(buildChildEnv(base)).toEqual(KEPT);
  });

  it("lists exactly the allowed keys and prefixes", () => {
    expect(ALLOWED_ENV_PREFIXES).toEqual(["LC_", "XDG_"]);
    expect([...ALLOWED_ENV_KEYS].sort()).toEqual(
      [
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
      ].sort(),
    );
  });

  it("adds explicit overrides on top of the allowed variables", () => {
    const env = buildChildEnv(
      { HOME: "/Users/x", PATH: "/bin", ANTHROPIC_PROFILE: "work", APP_LOCAL_MODE: "0" },
      { PATH: "/opt/homebrew/bin:/bin", APP_LOCAL_MODE: "1", API_PORT: "48100" },
    );
    expect(env).toEqual({ HOME: "/Users/x", PATH: "/opt/homebrew/bin:/bin", APP_LOCAL_MODE: "1", API_PORT: "48100" });
  });

  it("drops an inherited ELECTRON_RUN_AS_NODE but keeps an explicit one", () => {
    expect(buildChildEnv({ ELECTRON_RUN_AS_NODE: "1" })).not.toHaveProperty("ELECTRON_RUN_AS_NODE");
    expect(buildChildEnv({}, { ELECTRON_RUN_AS_NODE: "1" }).ELECTRON_RUN_AS_NODE).toBe("1");
  });

  it("drops an inherited PORT, and the API env pins PORT to the API port", () => {
    // The backend reads API_PORT first, but PORT is pinned too, so a stray PORT never moves the API.
    expect(buildChildEnv({ PORT: "3000", HOME: "/Users/x" })).toEqual({ HOME: "/Users/x" });
    expect(buildChildEnv({ PORT: "3000" }, { API_PORT: "48100", PORT: "48100" }).PORT).toBe("48100");
    const index = fs.readFileSync(path.join(__dirname, "..", "src", "main", "index.ts"), "utf8");
    expect(index).toMatch(/API_PORT: String\(ports\.api\),\s*(\/\/[^\n]*\n\s*)*PORT: String\(ports\.api\),/);
  });

  it("removes a key when the override is undefined", () => {
    expect(buildChildEnv({ HOME: "/Users/x" }, { HOME: undefined })).not.toHaveProperty("HOME");
  });
});

describe("isLoadedEnvFile", () => {
  it("matches the dotenv files that the backend and next dev load", () => {
    for (const name of [".env", ".env.local", ".env.development", ".env.development.local", ".env.production"]) {
      expect(isLoadedEnvFile(name), name).toBe(true);
    }
  });

  it("skips the example file and names that only start with .env", () => {
    for (const name of [".env.example", ".envrc", ".environment", "env", "app.env"]) {
      expect(isLoadedEnvFile(name), name).toBe(false);
    }
  });
});
