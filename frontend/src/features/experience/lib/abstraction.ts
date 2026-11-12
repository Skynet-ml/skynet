/*
 * How much of Skynet a user sees: one per-user level, stored on the server and
 * changed only in Settings. Every surface that the level touches asks this
 * table, so the gating lives in one place instead of scattered conditionals,
 * and the loading skeletons that mirror those surfaces ask the same table.
 *
 * Hiding a control never changes what a run does: a hidden setting keeps its
 * default and submits it as before. The level only decides what is on screen
 * and which disclosures start open.
 */

export type ExperienceLevel = "guided" | "standard" | "expert";

export const EXPERIENCE_LEVELS: readonly ExperienceLevel[] = ["guided", "standard", "expert"];

/**
 * The level an unknown, missing or not-yet-loaded value means. New users start
 * with the least to read; anyone who picked a level keeps it.
 */
export const DEFAULT_LEVEL: ExperienceLevel = "guided";

export function normalizeLevel(value: unknown): ExperienceLevel {
  return value === "guided" || value === "standard" || value === "expert" ? value : DEFAULT_LEVEL;
}

/** Every surface the level gates. */
export type Surface =
  // Submission wizard
  | "wizard.split"
  | "wizard.optimizer_settings"
  | "wizard.model_params"
  | "wizard.shinka_settings"
  // Run page tabs
  | "run.tab.data"
  | "run.tab.code"
  | "run.tab.logs"
  | "run.tab.config"
  // Dashboard
  | "dashboard.analytics"
  | "dashboard.jobs.optimization_id"
  | "dashboard.jobs.elapsed_seconds"
  // Logs
  | "logs.verbose";

/** Surfaces Guided takes off screen. Standard and Expert show everything. */
const HIDDEN_IN_GUIDED: ReadonlySet<Surface> = new Set<Surface>([
  "wizard.split",
  "wizard.optimizer_settings",
  "wizard.model_params",
  "wizard.shinka_settings",
  "run.tab.data",
  "run.tab.code",
  "run.tab.logs",
  "run.tab.config",
  "dashboard.analytics",
  "dashboard.jobs.optimization_id",
  "dashboard.jobs.elapsed_seconds",
]);

/** Disclosures (and defaults) Expert starts open; Standard starts them closed. */
const OPEN_IN_EXPERT: ReadonlySet<Surface> = new Set<Surface>([
  "wizard.optimizer_settings",
  "wizard.model_params",
  "wizard.shinka_settings",
  "logs.verbose",
]);

/** Whether `surface` is on screen at `level`. */
export function isVisible(surface: Surface, level: ExperienceLevel): boolean {
  return level !== "guided" || !HIDDEN_IN_GUIDED.has(surface);
}

/** Whether `surface` (a disclosure, or an on/off default) starts open at `level`. */
export function defaultOpen(surface: Surface, level: ExperienceLevel): boolean {
  return level === "expert" && OPEN_IN_EXPERT.has(surface);
}

const SURFACES: ReadonlySet<string> = new Set<string>([...HIDDEN_IN_GUIDED, ...OPEN_IN_EXPERT]);

function gate(prefix: string, level: ExperienceLevel): (key: string) => boolean {
  return (key) => {
    const surface = `${prefix}${key}`;
    return !SURFACES.has(surface) || isVisible(surface as Surface, level);
  };
}

/**
 * Which run-page tabs show at `level`. The loaded page and its skeleton both
 * take this predicate, so their tab bars always match.
 */
export function detailTabGate(level: ExperienceLevel): (tab: string) => boolean {
  return gate("run.tab.", level);
}

/** Which jobs-table columns show at `level`; shared by the jobs table and its skeleton. */
export function jobsColumnGate(level: ExperienceLevel): (key: string) => boolean {
  return gate("dashboard.jobs.", level);
}
