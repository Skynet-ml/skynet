import fs from "node:fs";
import path from "node:path";
import { app, BrowserWindow, clipboard, ipcMain, nativeTheme, screen, shell } from "electron";
import { APP_SCHEME, appUrlFromArgv, appWindowUrl, HOSTED_APP_ORIGINS, hostedAppOrigin, parseLinkCallback, webDocsUrl } from "./app-url";
import { HostedLinkClient } from "./hosted-link";
import { isOpenAppUrl, parseTheme, pickTheme, type Theme } from "./theme";
import { childPath, probeClis, probeDocker, readCliLimits, searchDirs } from "./cli-detect";
import { allocatePorts, type PortPlan } from "./ports";
import { holdQuitUntilStopped, stopServices } from "./quit";
import { databaseUrl, removeSandboxContainers, startPostgres, stopPostgres, type DockerCtx } from "./postgres";
import { ensureSandboxImage } from "./sandbox-image";
import { ensurePrivateDir, loadOrCreateSecrets } from "./secrets";
import { finishSteps, latestLine, loadStepTimes, newTimeline, saveStepTimes, startStep, type Timeline } from "./step-timing";
import { clearPids, psStartOf, recordPid, sweepOrphans } from "./stack-pids";
import { buildChildEnv, loginShellPath } from "./shell-env";
import {
  computeOnboardingState,
  httpStatus,
  killActiveCommands,
  ManagedProcess,
  runCommand,
  waitFor,
  type OnboardingState,
} from "./supervisor";
import { startUpdateCheck } from "./updater";

const dataDir = process.env.SKYNET_DESKTOP_DATA_DIR || app.getPath("userData");
const logsDir = path.join(dataDir, "logs");
const localStateDir = path.join(dataDir, "local");
ensurePrivateDir(dataDir);
fs.mkdirSync(logsDir, { recursive: true });
fs.mkdirSync(localStateDir, { recursive: true });
const desktopLog = fs.createWriteStream(path.join(logsDir, "desktop.log"), { flags: "a" });

function log(line: string): void {
  desktopLog.write(`${new Date().toISOString()} ${line}\n`);
}

/** Bundled stack: inside the .app when packaged, `desktop/stage` in development. */
const resourcesDir = app.isPackaged ? process.resourcesPath : path.join(__dirname, "..", "..", "stage");
const uvBin = app.isPackaged
  ? path.join(resourcesDir, "uv", "uv")
  : path.join(resourcesDir, "uv", process.arch === "arm64" ? "arm64" : "x64", "uv");

if (process.env.SKYNET_DESKTOP_THEME === "light" || process.env.SKYNET_DESKTOP_THEME === "dark") {
  nativeTheme.themeSource = process.env.SKYNET_DESKTOP_THEME;
}

// The web app picks its language from Accept-Language, which Chromium takes
// from its own locale (the system language). A forced onboarding language must
// change that locale too, or the web app stays in the system language.
if (process.env.SKYNET_DESKTOP_LANG === "en" || process.env.SKYNET_DESKTOP_LANG === "he") {
  app.commandLine.appendSwitch("lang", process.env.SKYNET_DESKTOP_LANG);
}

let win: BrowserWindow | null = null;
let state: OnboardingState = { kind: "checking", clis: [], docker: null };
let acceptedRateCap = false;
let busy = false;
let quitting = false;
let relaunchAfterQuit = false;
let dockerCtx: DockerCtx | null = null;
let api: ManagedProcess | null = null;
let frontend: ManagedProcess | null = null;
let postgresStarted = false;
let frontendUrl: string | null = null;
// The hosted app (docs links) for this build; replaced from the stamp when the stack starts.
let hostedOrigin: string = HOSTED_APP_ORIGINS.production;
let sweptOrphans = false;
let sweptSandboxes = false;
let hostedLink: HostedLinkClient | null = null;
let signInUrl: string | null = null;
let finishingSignIn = false;

// Setup screens are small; the window grows for the web app. The onboarding
// page reports its content height, so each setup screen has no empty space.
const SETUP_SIZE = { width: 600, height: 120 };
const SETUP_MAX_HEIGHT = 720;
let setupHeight = SETUP_SIZE.height;

const APP_SIZE = { width: 1440, height: 900, minWidth: 960, minHeight: 640 };

function setState(next: OnboardingState): void {
  state = next;
  log(`state ${next.kind}${next.step ? ` (${next.step})` : ""}${next.error ? `: ${next.error}` : ""}`);
  win?.webContents.send("onboarding:state", state);
}

function locale(): "en" | "he" {
  const forced = process.env.SKYNET_DESKTOP_LANG;
  if (forced === "en" || forced === "he") return forced;
  return app.getLocale().toLowerCase().startsWith("he") ? "he" : "en";
}

function useSetupSize(): void {
  if (!win) return;
  win.setMinimumSize(SETUP_SIZE.width, SETUP_SIZE.height);
  win.setContentSize(SETUP_SIZE.width, setupHeight);
  win.setResizable(false);
  win.setMaximizable(false);
  win.setFullScreenable(false);
  win.center();
}

function useAppSize(): void {
  if (!win) return;
  win.setResizable(true);
  win.setMaximizable(true);
  win.setFullScreenable(true);
  win.setMinimumSize(APP_SIZE.minWidth, APP_SIZE.minHeight);
  win.setSize(APP_SIZE.width, APP_SIZE.height);
  win.center();
}

/**
 * The theme the web app reports from its own setting. The setup page reports
 * none, so the system appearance decides there. nativeTheme.themeSource stays
 * untouched: it would change prefers-color-scheme and break the web app's
 * "system" mode.
 */
let reportedTheme: Theme | null = null;

const currentTheme = () => pickTheme(reportedTheme, nativeTheme.shouldUseDarkColors);

// Matches --bg in onboarding.css and the web app, so no light flash shows in dark mode.
const windowBackground = () => (currentTheme() === "dark" ? "#1c1814" : "#f5f0ea");

/**
 * The Helper binary runs Node without a Dock icon (its bundle sets LSUIElement);
 * the main binary in Node mode still shows up as a second app in the Dock.
 */
function nodeBinary(): string {
  const name = path.basename(process.execPath);
  const helper = path.join(path.dirname(process.execPath), "..", "Frameworks", `${name} Helper.app`, "Contents", "MacOS", `${name} Helper`);
  return fs.existsSync(helper) ? helper : process.execPath;
}

/** The Dock icon follows the app theme; the .icns in Finder stays light. */
function applyTheme(): void {
  app.dock?.setIcon(path.join(__dirname, "..", "icons", currentTheme() === "dark" ? "dock-dark.png" : "dock-light.png"));
  win?.setBackgroundColor(windowBackground());
}

/** `npm run dev`: reload the onboarding page or relaunch on a rebuild of out/. */
function watchOwnBuild(): void {
  const outDir = path.join(__dirname, "..");
  let timer: NodeJS.Timeout | null = null;
  const debounce = (fn: () => void) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(fn, 200);
  };
  fs.watch(path.join(outDir, "renderer"), () =>
    debounce(() => {
      if (win?.webContents.getURL().startsWith("file:")) win.webContents.reload();
    }),
  );
  for (const dir of ["main", "preload"]) {
    fs.watch(path.join(outDir, dir), () =>
      debounce(() => {
        log("dev: rebuilt, relaunching");
        app.relaunch();
        app.quit();
      }),
    );
  }
}

/** Set the state first: the window size follows it, and getURL() still has the old page. */
function showOnboarding(next?: OnboardingState): void {
  if (next) setState(next);
  reportedTheme = null;
  applyTheme();
  useSetupSize();
  void win?.loadFile(path.join(__dirname, "..", "renderer", "index.html"), { query: { lang: locale() } });
}

/**
 * Copy the bundled backend and frontend into Application Support once per
 * bundle. uv writes build metadata next to pyproject.toml and Next writes its
 * cache under .next, and neither may write inside the signed .app.
 */
function stageRuntime(): { backendDir: string; frontendDir: string; bundleId: string } {
  const bundleId = fs.readFileSync(path.join(resourcesDir, "bundle-id.txt"), "utf8").trim();
  const runtimeRoot = path.join(dataDir, "runtime");
  const target = path.join(runtimeRoot, bundleId);
  const marker = path.join(target, ".complete");
  if (!fs.existsSync(marker)) {
    fs.rmSync(target, { recursive: true, force: true });
    fs.mkdirSync(target, { recursive: true });
    fs.cpSync(path.join(resourcesDir, "backend"), path.join(target, "backend"), { recursive: true });
    fs.cpSync(path.join(resourcesDir, "frontend"), path.join(target, "frontend"), { recursive: true, verbatimSymlinks: true });
    fs.writeFileSync(marker, bundleId);
  }
  for (const old of fs.readdirSync(runtimeRoot)) {
    if (old !== bundleId) fs.rmSync(path.join(runtimeRoot, old), { recursive: true, force: true });
  }
  return { backendDir: path.join(target, "backend"), frontendDir: path.join(target, "frontend"), bundleId };
}

async function check(): Promise<void> {
  if (busy) return;
  busy = true;
  try {
    if (!sweptOrphans) {
      // Before stopStack, which deletes the pid file. Ports held by a crashed session are freed before allocatePorts.
      sweptOrphans = true;
      await sweepOrphans(dataDir, {
        startOf: psStartOf,
        kill: (target, sig) => process.kill(target, sig),
        sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
        log,
      });
    }
    await stopStack();
    setState({ kind: "checking", clis: [], docker: null });
    const login = await loginShellPath();
    const loginPath = childPath(login.path);
    const env = buildChildEnv(process.env, { PATH: loginPath });
    const dirs = searchDirs(login.path);
    const [docker, clis] = await Promise.all([probeDocker(dirs, env), probeClis(dirs, env)]);
    const next = computeOnboardingState({
      docker,
      clis,
      limits: readCliLimits(localStateDir),
      now: new Date(),
      acceptedRateCap,
    });
    setState(next);
    if (next.kind !== "starting") return;
    dockerCtx = { docker: docker.path as string, env };
    if (!sweptSandboxes) {
      // Job containers from a crashed session are not removed by its quit path.
      sweptSandboxes = true;
      await removeSandboxContainers(dockerCtx);
    }
    await startStack(next, loginPath);
  } catch (err) {
    if (quitting) return;
    const message = err instanceof Error ? err.message : String(err);
    setState({ ...state, kind: "error", error: message });
    await stopStack();
  } finally {
    busy = false;
  }
}

async function startStack(base: OnboardingState, loginPath: string): Promise<void> {
  let timeline: Timeline = newTimeline(loadStepTimes(dataDir));
  // The latest output line goes to the window at most a few times a second, and not to desktop.log.
  let pendingDetail: string | null = null;
  let detailTimer: NodeJS.Timeout | null = null;
  const step = (name: string) => {
    // A quit during first-launch setup must not start the next service after shutdown ran.
    if (quitting) throw new Error("Skynet is quitting");
    if (detailTimer) clearTimeout(detailTimer);
    detailTimer = null;
    pendingDetail = null;
    timeline = startStep(timeline, name, Date.now());
    setState({ ...base, kind: "starting", step: name, timeline });
  };
  const onOutput = (chunk: string) => {
    pendingDetail = latestLine(chunk) ?? pendingDetail;
    if (detailTimer || !pendingDetail) return;
    detailTimer = setTimeout(() => {
      detailTimer = null;
      if (state.kind !== "starting" || !pendingDetail) return;
      timeline = { ...timeline, detail: pendingDetail };
      state = { ...state, timeline };
      win?.webContents.send("onboarding:state", state);
    }, 300);
  };
  const ctx = dockerCtx as DockerCtx;
  const binEnv: Record<string, string> = { APP_DOCKER_BIN: ctx.docker };
  for (const cli of base.clis) {
    if (cli.path && cli.versionOk) binEnv[`APP_${cli.id.toUpperCase()}_BIN`] = cli.path;
  }

  step("secrets");
  const { secrets, created, replacedLegacy } = loadOrCreateSecrets(dataDir);
  log(created ? "created new local secrets" : "reused local secrets");
  if (replacedLegacy) log("replaced the keychain-encrypted secrets.bin; Postgres gets the new password");
  const ports: PortPlan = await allocatePorts();
  log(`ports api=${ports.api} frontend=${ports.frontend} postgres=${ports.postgres}`);

  step("runtime");
  const { backendDir, frontendDir, bundleId } = stageRuntime();
  const stampFile = path.join(backendDir, "core", "hosted_target.txt");
  hostedOrigin = hostedAppOrigin(fs.existsSync(stampFile) ? fs.readFileSync(stampFile, "utf8") : null);
  log(`hosted app ${hostedOrigin}`);

  step("postgres");
  postgresStarted = true;
  await startPostgres(ctx, ports.postgres, secrets.pgPassword, onOutput);

  step("sandbox");
  const sandboxImage = await ensureSandboxImage(ctx, backendDir, bundleId, path.join(logsDir, "sandbox-build.log"), onOutput);

  const feOrigin = `http://127.0.0.1:${ports.frontend}`;
  const apiOrigin = `http://127.0.0.1:${ports.api}`;
  const uvEnv = buildChildEnv(process.env, {
    PATH: loginPath,
    UV_PROJECT_ENVIRONMENT: path.join(dataDir, "venv"),
    UV_NO_PROGRESS: "1",
    PYTHONPYCACHEPREFIX: path.join(dataDir, "pycache"),
    APP_LOCAL_MODE: "1",
    API_HOST: "127.0.0.1",
    API_PORT: String(ports.api),
    // The backend reads API_PORT first and falls back to PORT. buildChildEnv
    // drops an inherited PORT, and this pins it to the same port, so the two
    // never disagree.
    PORT: String(ports.api),
    WORKER_ENABLED: "true",
    REMOTE_DB_URL: databaseUrl(secrets.pgPassword, ports.postgres),
    BACKEND_AUTH_SECRET: secrets.backendAuthSecret,
    ALLOWED_ORIGINS: feOrigin,
    APP_LOCAL_STATE_DIR: localStateDir,
    APP_LOCAL_SANDBOX_IMAGE: sandboxImage,
    ...binEnv,
  });

  step("python");
  const migrateLog = fs.createWriteStream(path.join(logsDir, "migrate.log"), { flags: "a" });
  try {
    const sync = await runCommand(uvBin, ["sync", "--frozen"], { env: uvEnv, cwd: backendDir, timeoutMs: 45 * 60_000, log: migrateLog, onOutput });
    if (sync.code !== 0) throw new Error(`uv sync failed; see ${path.join(logsDir, "migrate.log")}`);
    step("migrations");
    const migrate = await runCommand(uvBin, ["run", "--frozen", "alembic", "upgrade", "head"], {
      env: uvEnv,
      cwd: backendDir,
      timeoutMs: 10 * 60_000,
      log: migrateLog,
      onOutput,
    });
    if (migrate.code !== 0) throw new Error(`Database migrations failed; see ${path.join(logsDir, "migrate.log")}`);
  } finally {
    migrateLog.end();
  }

  step("api");
  const apiArgs = ["run", "--frozen", "python", "main.py"];
  api = new ManagedProcess("api", uvBin, apiArgs, {
    env: uvEnv,
    cwd: backendDir,
    logFile: path.join(logsDir, "api.log"),
  });
  api.start();
  recordStarted("api", api.pid);
  const apiProc = api;
  await waitFor("The API", async () => (await httpStatus(`${apiOrigin}/health`)) === 200, {
    timeoutMs: 180_000,
    alive: () => !apiProc.exited,
  });

  step("frontend");
  frontend = new ManagedProcess("frontend", nodeBinary(), ["server.js"], {
    env: buildChildEnv(process.env, {
      PATH: loginPath,
      ELECTRON_RUN_AS_NODE: "1",
      NODE_ENV: "production",
      PORT: String(ports.frontend),
      HOSTNAME: "127.0.0.1",
      APP_LOCAL_MODE: "1",
      API_URL: apiOrigin,
      AUTH_SECRET: secrets.authSecret,
      BACKEND_AUTH_SECRET: secrets.backendAuthSecret,
      AUTH_URL: feOrigin,
      APP_HOSTED_APP_URL: hostedOrigin,
    }),
    cwd: frontendDir,
    logFile: path.join(logsDir, "frontend.log"),
  });
  frontend.start();
  recordStarted("frontend", frontend.pid);
  const feProc = frontend;
  await waitFor("The web app", async () => (await httpStatus(`${feOrigin}/api/auth/session`)) === 200, {
    timeoutMs: 120_000,
    alive: () => !feProc.exited,
  });

  if (quitting) return;
  if (detailTimer) clearTimeout(detailTimer);
  try {
    saveStepTimes(dataDir, finishSteps(timeline, Date.now()));
  } catch (err) {
    log(`could not save step times: ${err instanceof Error ? err.message : String(err)}`);
  }
  frontendUrl = feOrigin;
  watchForCrash(apiProc, "The API");
  watchForCrash(feProc, "The web app");

  // Sign-in to a Skynet account comes first; "Use without signing in" skips it.
  hostedLink = new HostedLinkClient(apiOrigin, secrets.backendAuthSecret);
  await hostedLink.ensureLocalUser();
  let linked = false;
  try {
    linked = await hostedLink.linked();
  } catch (err) {
    log(`account status check failed: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (linked) return openApp();
  setState({ ...base, kind: "sign_in", signIn: "waiting" });
  await signIn();
}

async function openApp(): Promise<void> {
  signInUrl = null;
  if (quitting || !frontendUrl || (state.kind !== "starting" && state.kind !== "sign_in")) return;
  setState({ ...state, kind: "ready", url: frontendUrl, signIn: undefined });
  useAppSize();
  await loadAppUrl(frontendUrl);
}

/**
 * Open the hosted sign-in (skynetml.com) in the default browser. The page
 * links the account and opens skynet://link/callback, which brings the result
 * back to handleAppUrl. Without a hosted link (or on an error) the onboarding
 * card offers a retry and "Use without signing in".
 */
async function signIn(): Promise<void> {
  if (state.kind !== "sign_in" || !hostedLink) return;
  let result;
  try {
    result = await hostedLink.start();
  } catch (err) {
    result = { ok: false as const, reason: "failed" as const, detail: err instanceof Error ? err.message : String(err) };
  }
  if (state.kind !== "sign_in") return;
  if (!result.ok) {
    log(`sign-in start failed (${result.reason}): ${result.detail}`);
    setState({ ...state, signIn: result.reason, error: result.detail || undefined });
    return;
  }
  signInUrl = result.url;
  setState({ ...state, signIn: "waiting", error: undefined });
  await openSignInPage();
}

async function openSignInPage(): Promise<void> {
  if (!signInUrl) return;
  try {
    await shell.openExternal(signInUrl);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    log(`could not open the browser: ${detail}`);
    if (state.kind === "sign_in") setState({ ...state, signIn: "failed", error: detail });
  }
}

/** The browser opened skynet://link/callback: finish the link, or show the retry card. */
async function handleAppUrl(raw: string): Promise<void> {
  const callback = parseLinkCallback(raw);
  if (!callback) {
    // The local link callback page opens skynet://open to bring the app back.
    if (isOpenAppUrl(raw)) {
      if (win?.isMinimized()) win.restore();
      win?.focus();
      return;
    }
    return log("ignored an unknown skynet:// URL");
  }
  if (win?.isMinimized()) win.restore();
  win?.focus();
  if (state.kind !== "sign_in" || state.signIn !== "waiting" || !hostedLink || finishingSignIn) {
    return log(`ignored a sign-in callback (state ${state.kind})`);
  }
  // Only the browser tab this sign-in opened may end it; a stale or forged
  // callback would otherwise leave "waiting" and drop the real one.
  if (!signInUrl || callback.state !== new URL(signInUrl).searchParams.get("state")) {
    return log("ignored a sign-in callback for another sign-in");
  }
  if (callback.kind === "denied") {
    log("sign-in declined in the browser");
    return setState({ ...state, signIn: "declined", error: undefined });
  }
  if (callback.kind === "error") {
    log(`sign-in failed in the browser: ${callback.error}`);
    return setState({ ...state, signIn: "failed", error: undefined });
  }
  finishingSignIn = true;
  try {
    const result = await hostedLink.finish(callback.code, callback.state);
    if (result.ok) return void (await openApp());
    log(`sign-in finish failed: ${result.detail}`);
    if (state.kind === "sign_in") setState({ ...state, signIn: result.reason, error: result.reason === "failed" ? result.detail : undefined });
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    log(`sign-in finish failed: ${detail}`);
    if (state.kind === "sign_in") setState({ ...state, signIn: "failed", error: detail });
  } finally {
    finishingSignIn = false;
  }
}

function skipSignIn(): void {
  log("sign-in skipped");
  void openApp();
}

function watchForCrash(proc: ManagedProcess, label: string): void {
  const timer = setInterval(() => {
    // "starting" too: startStack checks the account after it starts the watchers.
    if (quitting || (state.kind !== "starting" && state.kind !== "ready" && state.kind !== "sign_in")) return clearInterval(timer);
    if (!proc.exited) return;
    clearInterval(timer);
    frontendUrl = null;
    showOnboarding({ ...state, kind: "error", error: `${label} stopped unexpectedly (exit ${proc.exitCode}). See ${logsDir}.` });
  }, 1_000);
}

function recordStarted(role: string, pid: number | undefined): void {
  const started = pid ? psStartOf(pid) : null;
  if (pid && started) recordPid(dataDir, { role, pgid: pid, started });
}

// The web server keeps no state, so it gets a short grace period.
const FRONTEND_STOP_GRACE_MS = 2_000;

/** Stop the web server and the API, then Postgres and the sandbox containers. */
async function stopStack(): Promise<void> {
  killActiveCommands();
  hostedLink = null;
  signInUrl = null;
  const fe = frontend;
  const be = api;
  const ctx = postgresStarted ? dockerCtx : null;
  frontend = null;
  api = null;
  frontendUrl = null;
  await stopServices({
    frontend: async () => fe?.stop(FRONTEND_STOP_GRACE_MS),
    api: async () => be?.stop(),
    postgres: async () => {
      if (ctx) await stopPostgres(ctx);
    },
    sandboxes: async () => {
      if (ctx) await removeSandboxContainers(ctx);
    },
  });
  postgresStarted = false;
  clearPids(dataDir);
}

/**
 * Close the window and hide the Dock icon at once. Closing the window also
 * ends its open requests, so the web server does not wait for them to stop.
 */
function hideForQuit(): void {
  for (const w of BrowserWindow.getAllWindows()) w.destroy();
  win = null;
  app.dock?.hide();
}

function isAllowedUrl(target: string): boolean {
  if (target.startsWith("file://")) return true;
  return frontendUrl !== null && appWindowUrl(target, frontendUrl) !== null;
}

function guardNavigation(event: Electron.Event, url: string): void {
  if (url.startsWith("file://")) return;
  const docsUrl = frontendUrl === null ? null : webDocsUrl(url, frontendUrl, hostedOrigin);
  const appUrl = frontendUrl === null ? null : appWindowUrl(url, frontendUrl);
  if (appUrl === url && docsUrl === null) return;
  event.preventDefault();
  if (docsUrl !== null) {
    void shell.openExternal(docsUrl);
  } else if (appUrl !== null) {
    loadAppUrl(appUrl).catch((err) => log(`app page load failed: ${err instanceof Error ? err.message : String(err)}`));
  } else if (/^https?:\/\//.test(url)) void shell.openExternal(url);
}

/**
 * Load a page in the app window. guardNavigation can stop a redirect of this
 * load and start its own; Electron then rejects this load with ERR_ABORTED,
 * which is not a failure.
 */
async function loadAppUrl(url: string): Promise<void> {
  try {
    await win?.loadURL(url);
  } catch (err) {
    if ((err as { code?: string }).code !== "ERR_ABORTED") throw err;
  }
}

function createWindow(): void {
  win = new BrowserWindow({
    ...SETUP_SIZE,
    useContentSize: true,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    show: false,
    title: "Skynet",
    backgroundColor: windowBackground(),
    webPreferences: {
      preload: path.join(__dirname, "..", "preload", "index.js"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  win.once("ready-to-show", () => win?.show());
  win.webContents.setWindowOpenHandler(({ url }) => {
    const docsUrl = frontendUrl === null ? null : webDocsUrl(url, frontendUrl, hostedOrigin);
    if (docsUrl !== null) void shell.openExternal(docsUrl);
    else if (/^https?:\/\//.test(url) && !isAllowedUrl(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", guardNavigation);
  // Frames (for example the sandboxed iframe of model-written HTML) keep their own redirects.
  win.webContents.on("will-redirect", (event) => {
    if (event.isMainFrame) guardNavigation(event, event.url);
  });
  win.on("closed", () => {
    win = null;
  });
  showOnboarding();
}

ipcMain.handle("onboarding:get-state", () => state);
ipcMain.handle("onboarding:recheck", () => {
  void check();
});
ipcMain.handle("onboarding:continue", () => {
  acceptedRateCap = true;
  void check();
});
ipcMain.handle("onboarding:copy", (_event, text: unknown) => {
  if (typeof text === "string" && text.length < 500) clipboard.writeText(text);
});
ipcMain.handle("onboarding:fit", (_event, height: unknown) => {
  if (typeof height !== "number" || !Number.isFinite(height)) return;
  // openApp sends "ready" to this page before loadURL replaces it; a fit from that
  // last render would shrink the app window back to the setup size.
  if (state.kind === "ready" || !win?.webContents.getURL().startsWith("file:")) return;
  const before = win.getBounds();
  // A programmatic move or resize skips the macOS on-screen constrain, so keep the
  // window inside the work area (below the menu bar, above the Dock). The page
  // scrolls when its content is taller than the window.
  const area = screen.getDisplayMatching(before).workArea;
  const frame = before.height - win.getContentBounds().height;
  const next = Math.min(SETUP_MAX_HEIGHT, area.height - frame, Math.max(SETUP_SIZE.height, Math.ceil(height)));
  if (next === setupHeight) return;
  setupHeight = next;
  // Grow or shrink around the window's own middle instead of centering it again,
  // so a window the user dragged aside stays where they put it.
  win.setContentSize(SETUP_SIZE.width, setupHeight);
  const after = win.getBounds();
  const centered = Math.round(before.y - (after.height - before.height) / 2);
  const y = Math.max(area.y, Math.min(centered, area.y + area.height - after.height));
  win.setPosition(before.x, y);
});
ipcMain.handle("onboarding:open-logs", () => shell.openPath(logsDir));
ipcMain.handle("onboarding:sign-in", () => {
  void signIn();
});
ipcMain.handle("onboarding:skip-sign-in", () => skipSignIn());
ipcMain.on("theme:changed", (event, value: unknown) => {
  const theme = parseTheme(value);
  if (event.sender !== win?.webContents || !theme || theme === reportedTheme) return;
  reportedTheme = theme;
  applyTheme();
});
ipcMain.handle("onboarding:open-docker", () => shell.openExternal("https://www.docker.com/products/docker-desktop/"));

/**
 * Make this app the handler of skynet:// URLs. macOS takes the scheme from
 * CFBundleURLTypes in Info.plist (electron-builder.yml `protocols`; the dev
 * copy gets it in scripts/bundle-resources.mjs). In `electron .` the app path
 * is an argument, which the Windows and Linux handlers need.
 */
function registerAppScheme(): void {
  const ok = process.defaultApp && process.argv[1]
    ? app.setAsDefaultProtocolClient(APP_SCHEME, process.execPath, [path.resolve(process.argv[1])])
    : app.setAsDefaultProtocolClient(APP_SCHEME);
  log(`${APP_SCHEME}:// handler ${ok ? "registered" : "not registered"}`);
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  // Registered before ready: macOS sends the URL of a cold launch early.
  app.on("open-url", (event, url) => {
    event.preventDefault();
    void handleAppUrl(url);
  });
  app.on("second-instance", (_event, argv) => {
    // The window is already gone while the stack stops. Open a new app after
    // the exit instead of dropping the launch.
    if (quitting) {
      if (!relaunchAfterQuit) app.relaunch();
      relaunchAfterQuit = true;
      return;
    }
    if (win?.isMinimized()) win.restore();
    win?.focus();
    const url = appUrlFromArgv(argv);
    if (url) void handleAppUrl(url);
  });
  app.whenReady().then(() => {
    log(`Skynet desktop ${app.getVersion()} starting`);
    registerAppScheme();
    // Secure DNS first, the system resolver as fallback: some networks fail to
    // resolve the hosted sign-in page through the system resolver.
    app.configureHostResolver({ secureDnsMode: "automatic", secureDnsServers: ["https://cloudflare-dns.com/dns-query"] });
    createWindow();
    applyTheme();
    nativeTheme.on("updated", applyTheme);
    if (process.env.SKYNET_DESKTOP_DEV === "1" && !app.isPackaged) watchOwnBuild();
    win?.webContents.once("did-finish-load", () => void check());
    startUpdateCheck(log);
  });
  app.on("window-all-closed", () => app.quit());
  app.on(
    "before-quit",
    holdQuitUntilStopped(
      () => {
        quitting = true;
        const started = Date.now();
        stopStack()
          .catch((err: Error) => log(`shutdown error: ${err.message}`))
          .finally(() => {
            log(`stopped in ${Date.now() - started} ms`);
            desktopLog.end(() => app.exit(0));
          });
      },
      hideForQuit,
    ),
  );
}
