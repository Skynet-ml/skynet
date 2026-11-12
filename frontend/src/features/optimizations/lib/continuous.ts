/*
 * Pure helpers for the Continuous tab: form checks that mirror the backend's
 * enable rules, and how a check's outcome reads in the history.
 */

import type {
  ContinuousCycle,
  ContinuousCycleStatus,
  ContinuousPermitted,
  ContinuousPlan,
} from "@/shared/types/continuous";

/** Visual weight of a check's outcome. */
export type CycleTone = "good" | "warn" | "bad" | "muted" | "busy";

const TONES: Record<ContinuousCycleStatus, CycleTone> = {
  checking: "busy",
  optimizing: "busy",
  awaiting_review: "good",
  approved: "good",
  skipped: "muted",
  rejected: "muted",
  paused: "warn",
  regressed: "warn",
  failed: "bad",
};

export function cycleTone(status: ContinuousCycleStatus): CycleTone {
  return TONES[status] ?? "muted";
}

export interface ModelChange {
  role: string;
  from: string | null;
  to: string;
}

/**
 * Each model a check's run swapped, with its old and new name; an old name it
 * did not record is null. A check that stopped before its run started (paused,
 * no test split, rejected) swapped nothing, and a check saved before its models
 * were kept has no names to show, so neither lists a change.
 */
export function modelChanges(cycle: ContinuousCycle): ModelChange[] {
  if (!cycle.optimization_id) return [];
  const snapshot = cycle.snapshot ?? {};
  const newModels = (snapshot.new_models ?? {}) as Record<string, { name?: string } | undefined>;
  const oldModels = (snapshot.old_models ?? {}) as Record<string, string | undefined>;
  return cycle.changes.flatMap((role) => {
    const to = newModels[role]?.name;
    return to ? [{ role, from: oldModels[role] ?? null, to }] : [];
  });
}

/**
 * The old and new model names without their provider path, or in full when
 * the short names would be empty or the same and so hide the change.
 */
export function modelLabels(from: string | null, to: string): [string | null, string] {
  const short = (name: string) => name.split("/").pop() ?? "";
  const shortFrom = from === null ? null : short(from);
  const shortTo = short(to);
  if (shortFrom === "" || shortTo === "" || shortFrom === shortTo) return [from, to];
  return [shortFrom, shortTo];
}

/**
 * The score change as shown (three decimals), or null without both scores.
 * Each score is rounded first, so the gain always matches the two scores shown beside it.
 */
export function scoreGain(cycle: ContinuousCycle): number | null {
  if (cycle.incumbent_score == null || cycle.candidate_score == null) return null;
  // toFixed, not Math.round(x * 1000): the two disagree on half-way scores such as 0.1235.
  const shown = (score: number) => Math.round(Number(score.toFixed(3)) * 1000);
  return (shown(cycle.candidate_score) - shown(cycle.incumbent_score)) / 1000;
}

/** Only a validated candidate waits for the owner's approve or reject. */
export function canReview(cycle: ContinuousCycle): boolean {
  return cycle.status === "awaiting_review" && !!cycle.optimization_id;
}

const DUE_REFRESH_MS = 5_000;
const RUN_REFRESH_MS = 30_000;
// Far-off checks are re-armed daily; a longer setTimeout delay overflows.
const MAX_WAIT_MS = 86_400_000;

/**
 * How long to wait before reloading the plan so the history shows a check's
 * outcome without a page reload; null when nothing is pending. A due check
 * (a scheduled time that passed) or one still checking is a sweep away,
 * while a run takes minutes. A tab left open waits for the next scheduled
 * check.
 */
export function refreshDelayMs(plan: ContinuousPlan, now: number): number | null {
  if (plan.cycles.some((c) => c.status === "checking")) return DUE_REFRESH_MS;
  const due = plan.next_check_at ? Date.parse(plan.next_check_at) : Number.NaN;
  if (plan.status === "active" && due <= now) return DUE_REFRESH_MS;
  if (plan.cycles.some((c) => c.status === "optimizing")) return RUN_REFRESH_MS;
  if (plan.status === "active" && due > now)
    return Math.min(due - now + DUE_REFRESH_MS, MAX_WAIT_MS);
  return null;
}

export interface EnableForm {
  permitted: ContinuousPermitted;
}

/** Why the enable form cannot be sent, as a UI message key; null when it can. */
export type EnableProblem = "continuous_tab.form.need_permission";

export function enableProblem(form: EnableForm): EnableProblem | null {
  // Repository runs switch only the reflection model, so a plan without it never changes.
  if (!form.permitted.reflection_model) return "continuous_tab.form.need_permission";
  return null;
}
