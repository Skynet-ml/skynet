import en from "./locales/en.json";
import he from "./locales/he.json";
import { formatClock, runningView, stepPhase, stepTime as formatStepTime, totalElapsed, type Timeline } from "./timeline";

type Kind =
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

interface Cli {
  id: "claude" | "codex";
  path: string | null;
  version: string | null;
  minVersion: string;
  versionOk: boolean;
  loggedIn: boolean | null;
  loginCommand: string;
  upgradeCommand: string;
}

interface State {
  kind: Kind;
  clis: Cli[];
  docker: { path: string | null; running: boolean; error: string | null } | null;
  resetAt?: string;
  limitMessage?: string;
  step?: string;
  timeline?: Timeline;
  error?: string;
  signIn?: "waiting" | "unavailable" | "unreachable" | "failed" | "declined";
}

interface Bridge {
  getState(): Promise<State>;
  recheck(): Promise<void>;
  continueAnyway(): Promise<void>;
  copy(text: string): Promise<void>;
  fit(height: number): Promise<void>;
  openLogs(): Promise<void>;
  openDockerDownload(): Promise<void>;
  signIn(): Promise<void>;
  onState(listener: (state: State) => void): () => void;
}

declare global {
  interface Window {
    skynetDesktop: Bridge;
  }
}

const lang = new URLSearchParams(window.location.search).get("lang") === "he" ? "he" : "en";
const messages: Record<string, string> = lang === "he" ? he : en;
document.documentElement.lang = lang;
document.documentElement.dir = lang === "he" ? "rtl" : "ltr";

const CLI_NAMES: Record<Cli["id"], string> = { claude: "Claude Code", codex: "Codex CLI" };
const INSTALL_COMMANDS: Record<Cli["id"], string> = {
  claude: "npm install -g @anthropic-ai/claude-code",
  codex: "npm install -g @openai/codex",
};

function t(key: string, vars: Record<string, string> = {}): string {
  const text = messages[key] ?? en[key as keyof typeof en] ?? key;
  return text.replace(/\{(\w+)\}/g, (_, name: string) => vars[name] ?? "");
}

const seconds = (n: string) => t("timeline.seconds", { n });

function stepTime(ms: number): string {
  return formatStepTime(ms, seconds);
}

function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: { className?: string; text?: string; dir?: string } = {},
  children: Node[] = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (props.className) node.className = props.className;
  if (props.text !== undefined) node.textContent = props.text;
  if (props.dir) node.dir = props.dir;
  for (const child of children) node.append(child);
  return node;
}

function button(label: string, variant: "primary" | "secondary", onClick: () => void): HTMLButtonElement {
  const b = el("button", { className: `btn btn-${variant}`, text: label });
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

/** A shell command with a copy button. Commands stay left-to-right in Hebrew too. */
function command(text: string, label?: string): HTMLElement {
  const copy = button(t("action.copy"), "secondary", () => {
    void window.skynetDesktop.copy(text);
    copy.textContent = t("action.copied");
    setTimeout(() => (copy.textContent = t("action.copy")), 1500);
  });
  copy.classList.add("btn-small");
  const row = el("div", { className: "cmd" }, [el("code", { text, dir: "ltr" }), copy]);
  return label ? el("div", { className: "cmd-group" }, [el("div", { className: "cmd-label", text: label }), row]) : row;
}

type RowStatus = "ok" | "fail" | "active" | "pending";

const CHECK_ORDER: Kind[][] = [
  ["docker_missing", "docker_stopped"],
  ["no_cli", "cli_too_old"],
  ["not_logged_in"],
  ["rate_capped"],
  ["starting", "error"],
  ["sign_in", "ready"],
];

function rowStatuses(state: State): { key: string; status: RowStatus }[] {
  const keys = ["check.docker", "check.cli", "check.login", "check.limits", "check.stack", "check.account"];
  const at = CHECK_ORDER.findIndex((kinds) => kinds.includes(state.kind));
  return keys.map((key, i) => {
    if (state.kind === "checking") return { key, status: i === 0 ? "active" : "pending" };
    if (i < at || (i === at && state.kind === "ready")) return { key, status: "ok" };
    if (i === at) return { key, status: state.kind === "starting" || state.kind === "sign_in" ? "active" : "fail" };
    return { key, status: "pending" };
  });
}

function renderChecklist(state: State): void {
  const list = document.getElementById("checklist") as HTMLOListElement;
  list.replaceChildren(
    ...rowStatuses(state).map(({ key, status }) =>
      el("li", { className: `check check-${status}` }, [
        el("span", { className: "dot", text: status === "ok" ? "✓" : status === "fail" ? "!" : "" }),
        el("span", { className: "check-label", text: t(key) }),
      ]),
    ),
  );
}

// The running step's clock and bar update from here; render() replaces it.
let tick: (() => void) | null = null;
setInterval(() => tick?.(), 250);

function timelinePanel(timeline: Timeline): HTMLElement {
  const elapsedNode = el("span", { className: "tl-elapsed", dir: "ltr" });
  const head = el("div", { className: "tl-head" }, [
    el("span", { className: "tl-title", text: t("starting.title") }),
    el("span", { className: "tl-totals" }, [elapsedNode]),
  ]);
  const updates: ((now: number) => void)[] = [];

  const rows = timeline.steps.map((step) => {
    const phase = stepPhase(step);
    const time = el("span", { className: "tl-time", dir: "ltr" });
    const row = el("li", { className: `tl-step tl-${phase}` }, [
      el("span", { className: "tl-icon", text: phase === "done" ? "✓" : "" }),
      el("span", { className: "tl-label", text: t(`step.${step.name}`) }),
      time,
    ]);
    const estimate = step.estimateMs;
    if (phase === "done") time.textContent = stepTime(step.durationMs as number);
    if (phase === "pending" && estimate !== undefined) time.textContent = `~${stepTime(estimate)}`;
    if (phase !== "active") return row;

    const startedAt = step.startedAt as number;
    if (estimate !== undefined) {
      const bar = el("div", { className: "tl-bar" }, [el("div", { className: "tl-sweep" })]);
      const hasFill = runningView(0, estimate, seconds).fill;
      if (hasFill) {
        const fill = el("div", { className: "tl-fill" });
        // A CSS animation fills the bar; the negative delay resumes it after a re-render without a jump.
        fill.style.animationDuration = `${estimate}ms`;
        fill.style.animationDelay = `-${Math.min(Date.now() - startedAt, estimate)}ms`;
        bar.prepend(fill);
      }
      // Without a fill, the bar stays hidden until the step passes its estimate.
      row.append(el("div", { className: hasFill ? "tl-extra" : "tl-extra tl-late" }, [bar]));
    }
    updates.push((now) => {
      const view = runningView(now - startedAt, estimate, seconds);
      time.textContent = view.time;
      row.classList.toggle("tl-over", view.over);
    });
    return row;
  });

  updates.push((now) => {
    elapsedNode.textContent = formatClock(totalElapsed(timeline, now));
  });
  tick = () => {
    const now = Date.now();
    for (const update of updates) update(now);
  };
  tick();

  const card = el("div", { className: "timeline" }, [head]);
  // The clock changes every second; screen readers hear the checklist, not every tick.
  card.setAttribute("aria-live", "off");
  card.append(el("ol", { className: "tl-steps" }, rows));
  return card;
}

function formatReset(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat(lang === "he" ? "he-IL" : "en-US", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function panelBody(state: State): Node[] {
  const bridge = window.skynetDesktop;
  const recheck = button(t("action.recheck"), "primary", () => void bridge.recheck());
  const nodes: Node[] = [];
  const actions = (...buttons: HTMLButtonElement[]) => el("div", { className: "actions" }, buttons);

  switch (state.kind) {
    case "checking":
      break;
    case "docker_missing":
      nodes.push(actions(button(t("docker_missing.action"), "primary", () => void bridge.openDockerDownload()), button(t("action.recheck"), "secondary", () => void bridge.recheck())));
      break;
    case "docker_stopped":
      if (state.docker?.error) nodes.push(el("pre", { className: "detail", text: state.docker.error, dir: "ltr" }));
      nodes.push(actions(recheck));
      break;
    case "no_cli":
      for (const id of ["claude", "codex"] as const) nodes.push(command(INSTALL_COMMANDS[id], CLI_NAMES[id]));
      nodes.push(actions(recheck));
      break;
    case "cli_too_old":
      for (const cli of state.clis.filter((c) => c.path && !c.versionOk)) {
        const found = t("cli_too_old.found", { cli: CLI_NAMES[cli.id], version: cli.version ?? "?", min: cli.minVersion });
        nodes.push(command(cli.upgradeCommand, found));
      }
      nodes.push(actions(recheck));
      break;
    case "not_logged_in":
      for (const cli of state.clis.filter((c) => c.versionOk && c.loggedIn !== true)) {
        const label = cli.id === "codex" ? t("not_logged_in.codex", { cli: CLI_NAMES.codex }) : CLI_NAMES[cli.id];
        nodes.push(command(cli.loginCommand, label));
      }
      nodes.push(actions(recheck));
      break;
    case "rate_capped":
      if (state.resetAt) nodes.push(el("p", { className: "reset", text: t("rate_capped.reset", { time: formatReset(state.resetAt) }) }));
      if (state.limitMessage) nodes.push(el("pre", { className: "detail", text: state.limitMessage, dir: "auto" }));
      nodes.push(actions(button(t("rate_capped.continue"), "primary", () => void bridge.continueAnyway()), button(t("action.recheck"), "secondary", () => void bridge.recheck())));
      break;
    case "starting":
      if (state.timeline) nodes.push(timelinePanel(state.timeline));
      break;
    case "sign_in": {
      if (state.signIn === "waiting") {
        // The browser shows the link page; the window shows only the timeline.
      } else if (state.signIn === "unavailable") {
        nodes.push(el("pre", { className: "detail", text: t("sign_in.unavailable") }));
      } else {
        if (state.signIn === "declined") nodes.push(el("p", { className: "lead", text: t("sign_in.declined") }));
        if (state.signIn === "unreachable") nodes.push(el("p", { className: "lead", text: t("sign_in.unreachable") }));
        if (state.signIn === "failed") nodes.push(el("pre", { className: "detail detail-error", text: state.error || t("sign_in.failed"), dir: "auto" }));
        nodes.push(actions(button(t("action.retry"), "primary", () => void bridge.signIn())));
      }
      break;
    }
    case "ready":
      break;
    case "error":
      if (state.error) nodes.push(el("pre", { className: "detail detail-error", text: state.error, dir: "ltr" }));
      nodes.push(actions(button(t("action.retry"), "primary", () => void bridge.recheck()), button(t("action.logs"), "secondary", () => void bridge.openLogs())));
      break;
  }
  return nodes;
}

const PROGRESS: Kind[] = ["checking", "starting", "ready"];
const isProgress = (state: State) =>
  PROGRESS.includes(state.kind) || (state.kind === "sign_in" && state.signIn === "waiting");

function render(state: State): void {
  tick = null;
  document.body.dataset.state = state.kind;
  document.body.toggleAttribute("data-progress", isProgress(state));
  renderChecklist(state);
  const lead = `${state.kind}.body`;
  const intro = document.getElementById("intro") as HTMLDivElement;
  // Progress screens show only the brand and the timeline; the timeline says what is happening.
  intro.hidden = isProgress(state);
  intro.replaceChildren(
    el("h1", { text: t(`${state.kind}.title`) }),
    ...(lead in messages || lead in en ? [el("p", { className: "lead", text: t(lead) })] : []),
  );
  const body = panelBody(state);
  const panel = document.getElementById("panel") as HTMLDivElement;
  panel.hidden = body.length === 0;
  panel.replaceChildren(...body);
}

for (const node of document.querySelectorAll<HTMLElement>("[data-i18n]")) {
  node.textContent = t(node.dataset.i18n as string);
}

// The window takes the height of the content, so no screen shows empty space.
const shell = document.querySelector(".shell") as HTMLElement;
const setup = document.querySelector(".setup") as HTMLElement;
new ResizeObserver(() => {
  const style = getComputedStyle(shell);
  void window.skynetDesktop.fit(setup.offsetHeight + parseFloat(style.paddingTop) + parseFloat(style.paddingBottom));
}).observe(setup);

window.skynetDesktop.onState(render);
void window.skynetDesktop.getState().then(render);
