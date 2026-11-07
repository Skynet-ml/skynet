/*
 * The metric-hardening job as the API returns it. Mirrors
 * `backend/core/models/hardening.py`; keep the two in step.
 */

export type HardeningStatus =
  | "preflight"
  | "preflight_failed"
  | "running"
  | "waiting_review"
  | "waiting_fix"
  | "clean_stop"
  | "budget_stop"
  | "done"
  | "rejected"
  | "failed"
  | "stale";

export type HardeningView = "black_box" | "white_box";
export type HardeningPlace = "local" | "cloud";
export type HardeningCheckResult = "pass" | "fail" | "pending";
export type HardeningRoundStatus =
  | "running"
  | "waiting_review"
  | "waiting_fix"
  | "clean"
  | "approved"
  | "fixed"
  | "rejected"
  | "stopped";

/** One attacker claim, with the judge's label and the user's verdict. */
export interface HardeningExploit {
  id: string;
  /** A "case" is one concrete input; a "method" is a general trick. */
  kind: "case" | "method";
  label: "exploit" | "valid";
  confidence: number;
  reason: string;
  unsure: boolean;
  patch_diff: string;
  why_md: string;
  user_verdict: "confirmed" | "rejected" | null;
}

export interface HardeningFixChecks {
  suite_replay: HardeningCheckResult;
  base_score_unchanged: HardeningCheckResult;
  /** Null when the metric has no reference solution. */
  reference_not_lower: HardeningCheckResult | null;
}

export interface HardeningFix {
  diff: string;
  checks: HardeningFixChecks;
  user_decision: "approved" | "rejected" | null;
}

export interface HardeningRound {
  index: number;
  status: HardeningRoundStatus;
  exploits: HardeningExploit[];
  fix: HardeningFix | null;
}

export interface HardeningJob {
  id: string;
  status: HardeningStatus;
  round: number;
  rounds: number;
  round_cap_usd: number;
  reserved_usd: number;
  spent_usd: number;
  place: HardeningPlace;
  attacker_model: string;
  judge_model: string;
  view: HardeningView;
  preflight_failures: string[];
  rounds_detail: HardeningRound[];
  suite_count: number;
  fixed_count: number;
  error: string | null;
}

export interface CreateHardeningJobRequest {
  draft_id: string;
  rounds: number;
  round_cap_usd: number;
  attacker_model: string;
  judge_model: string;
  view: HardeningView;
}

/** The statuses that end a job well; the overview reports the attacks it survived. */
export const HARDENING_SURVIVED: ReadonlySet<HardeningStatus> = new Set([
  "clean_stop",
  "budget_stop",
  "done",
]);

/** Every attacker claim the job judged, across its rounds: the attacks the metric faced. */
export function hardeningAttacks(job: HardeningJob): number {
  return job.rounds_detail.reduce((sum, round) => sum + round.exploits.length, 0);
}
