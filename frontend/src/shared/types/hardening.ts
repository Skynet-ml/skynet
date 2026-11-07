/*
 * The metric-hardening job as the API returns it. Mirrors
 * `backend/core/models/hardening.py`; keep the two in step.
 */

import type { BlackboxScorer, BlackboxTarget } from "./api";

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
  /** The repository target the create request named, as JSON. Jobs from before SKY-368 lack it. */
  target?: BlackboxTarget | null;
  /** The execution budget the job charges. */
  binding_id?: string | null;
}

export interface CreateHardeningJobRequest {
  draft_id: string;
  rounds: number;
  round_cap_usd: number;
  attacker_model: string;
  judge_model: string;
  view: HardeningView;
  /** A repository target only; its secrets may only name saved secrets. */
  target?: BlackboxTarget | null;
  binding_id?: string | null;
  /** The run submit's scorer; the runner scores each round with it. */
  scorer?: BlackboxScorer | null;
}

/** The statuses that end a job well; the overview reports the attacks it survived. */
export const HARDENING_SURVIVED: ReadonlySet<HardeningStatus> = new Set([
  "clean_stop",
  "budget_stop",
  "done",
]);

/**
 * The attacks the metric withstood, across its rounds: a claim the judge called
 * valid and the user did not confirm, a claim the user rejected, or a confirmed
 * exploit whose round ended in an approved fix. A confirmed exploit in a round
 * that never got its fix (a budget stop) does not count.
 */
export function hardeningSurvived(job: HardeningJob): number {
  return job.rounds_detail.reduce(
    (sum, round) =>
      sum +
      round.exploits.filter(
        (e) =>
          e.user_verdict === "rejected" ||
          (e.label === "valid" && e.user_verdict !== "confirmed") ||
          (e.user_verdict === "confirmed" && round.status === "fixed"),
      ).length,
    0,
  );
}
