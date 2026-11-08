import type { ContinuousEnableRequest } from "@/shared/types/continuous";

import type { WizardV2State } from "./wizard-v2";

/**
 * Where the open setup is kept for its tab, so a reload lands on the same
 * step with the same answers. A new name on purpose: the retired drafts'
 * keys are wiped on every visit.
 */
export const WIZARD_SESSION_KEY = "skynet.wizard.session.v1";

/**
 * What the setup's screens hold beside its answers, kept for the same tab and
 * read back only when the setup itself came back: the screen on view inside
 * the step and the scheduled run saved for the run.
 */
export const WIZARD_SCREEN_KEY = "skynet.wizard.screen.v1";
export const WIZARD_SCHEDULE_KEY = "skynet.wizard.schedule.v1";
// The setup's hardening job is found by this id. It goes with the setup, so a
// submit or Start new drops it and the next setup never shows the old job.
export const WIZARD_HARDENING_KEY = "skynet.wizard.hardening-draft.v1";
export const WIZARD_VIEW_KEYS = [WIZARD_SCREEN_KEY, WIZARD_SCHEDULE_KEY, WIZARD_HARDENING_KEY] as const;

const SLICES = ["task", "inputs", "optimizer", "workspace", "meta"] as const;

/**
 * Serialize a setup for the tab's session storage, without the values of
 * its repository secrets.
 *
 * Args:
 *   account: Who the setup belongs to.
 *   state: The wizard's state.
 *
 * Returns:
 *   The stored text.
 */
export function encodeWizardSession(account: string, state: WizardV2State): string {
  const repo = state.task.repo;
  // A secret value never goes to storage; after a reload the user types it again.
  const kept = repo?.secrets?.length
    ? {
        ...state,
        task: { ...state.task, repo: { ...repo, secrets: repo.secrets.map((secret) => ({ ...secret, value: "" })) } },
      }
    : state;
  return JSON.stringify({ account, state: kept });
}

/**
 * Read back a setup kept for this tab, or nothing when it is missing,
 * malformed, from another account or from an older shape.
 *
 * Args:
 *   raw: The stored text, or null.
 *   account: The signed-in account.
 *   steps: The wizard's step ids.
 *
 * Returns:
 *   The setup, or null.
 */
export function decodeWizardSession(
  raw: string | null,
  account: string,
  steps: readonly string[],
): WizardV2State | null {
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const { account: owner, state } = parsed as { account?: unknown; state?: unknown };
  if (owner !== account || !state || typeof state !== "object") return null;
  const s = state as Record<string, unknown>;
  if (typeof s.step !== "string" || !steps.includes(s.step)) return null;
  if (typeof s.furthestStep !== "string" || !steps.includes(s.furthestStep)) return null;
  if (!SLICES.every((slice) => s[slice] && typeof s[slice] === "object")) return null;
  return state as WizardV2State;
}

/**
 * Read one of the tab's saved values, or null when storage is blocked.
 *
 * Args:
 *   key: The session storage key.
 *
 * Returns:
 *   The stored text, or null.
 */
export function readSessionValue(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * Keep a value for the tab; full or blocked storage just drops it.
 *
 * Args:
 *   key: The session storage key.
 *   value: The text to keep.
 */
export function keepSessionValue(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {}
}

/**
 * The index of the screen a reload reopens inside the step: the saved one
 * while the step still has it, else the step's first.
 *
 * Args:
 *   screens: The step's screens, in order.
 *   saved: The screen on view before the reload, or null.
 *
 * Returns:
 *   The screen's index.
 */
export function reopenedPage(screens: readonly string[], saved: string | null): number {
  return saved === null ? 0 : Math.max(0, screens.indexOf(saved));
}

/**
 * Read back the scheduled run saved on the setup's schedule screen, or none
 * when it is missing or malformed.
 *
 * Args:
 *   raw: The stored text, or null.
 *
 * Returns:
 *   The saved plan request, or null.
 */
export function decodeScheduledRun(raw: string | null): ContinuousEnableRequest | null {
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const { schedule, permitted } = parsed as Record<string, unknown>;
  if (!schedule || typeof schedule !== "object") return null;
  if (!permitted || typeof permitted !== "object") return null;
  const { task_model, reflection_model } = permitted as Record<string, unknown>;
  // A draft saved before the model switches existed has neither, so it no longer describes a plan.
  if (typeof task_model !== "boolean" || typeof reflection_model !== "boolean") return null;
  return { schedule: schedule as ContinuousEnableRequest["schedule"], permitted: { task_model, reflection_model } };
}

/**
 * Whether a left setup holds anything worth offering back: an answer, data,
 * a repository or a step past the first.
 *
 * Args:
 *   state: The wizard's state as the user left it.
 *
 * Returns:
 *   True when a return to the wizard should offer it back.
 */
export function setupWorthKeeping(state: WizardV2State): boolean {
  return (
    state.furthestStep !== "task" ||
    Boolean(state.task.objective.trim() || state.task.background.trim() || state.meta.name.trim()) ||
    state.task.repo !== null ||
    state.inputs.cases.length > 0 ||
    state.inputs.stagedDatasetId !== null
  );
}
