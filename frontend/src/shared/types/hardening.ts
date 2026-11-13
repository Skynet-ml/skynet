/*
 * The metric-hardening job as the API returns it. Mirrors
 * `backend/core/models/hardening.py`; keep the two in step.
 */

import type { AgentMessage } from "../ui/agent/types";
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
type HardeningPlace = "local" | "cloud";
export type HardeningCheckResult = "pass" | "fail" | "pending";
type HardeningRoundStatus =
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
  /** The claim's score on the metric of its round; null when the scorer failed. */
  exploit_score?: number | null;
  /** The base solution's score on the same metric. */
  base_score?: number | null;
  /** The session's validated tree; null means the card shows patch_diff and why_md. */
  edge_case?: EdgeCaseNode[] | null;
  /** The 1-based session that found the claim. */
  session?: number | null;
}

/*
 * The edge-case catalog nodes (SKY-682). Mirrors the node models in
 * `backend/core/worker/hardening_sessions.py`; keep the two in step. A tree is
 * a list of at most 30 nodes with a depth of at most 2: only Steps holds
 * other nodes, and a Steps inside a Steps holds only text.
 */
export interface ScoreCompareNode {
  type: "ScoreCompare";
  exploit_score: number;
  base_score: number;
  /** Absent when the session gave none; the page then shows its own translated label. */
  exploit_label?: string;
  base_label?: string;
}

export interface DiffNode {
  type: "Diff";
  diff: string;
}

export interface IOPairNode {
  type: "IOPair";
  input: string;
  expected: string;
  got: string;
}

/** At most 20 columns and 50 rows; each row has one cell for each column. */
export interface TableNode {
  type: "Table";
  columns: string[];
  rows: Array<Array<string | number | boolean | null>>;
}

export interface CalloutNode {
  type: "Callout";
  tone: "info" | "warn" | "danger";
  text: string;
}

/** At most 200 points. */
export interface ChartNode {
  type: "Chart";
  kind: "bar" | "line";
  points: Array<{ x: string | number; y: number }>;
  title?: string;
}

export interface TextNode {
  type: "Text";
  text: string;
}

export interface InnerStepsNode {
  type: "Steps";
  items: string[];
}

type EdgeCaseLeafNode =
  | ScoreCompareNode
  | DiffNode
  | IOPairNode
  | TableNode
  | CalloutNode
  | ChartNode
  | TextNode
  | InnerStepsNode;

export interface StepsNode {
  type: "Steps";
  items: Array<string | EdgeCaseLeafNode>;
}

export type EdgeCaseNode =
  | ScoreCompareNode
  | DiffNode
  | IOPairNode
  | TableNode
  | CalloutNode
  | ChartNode
  | TextNode
  | StepsNode;

/** One attacker session of a round; its transcript uses the setup agent chat's entry shape. */
export interface HardeningSession {
  index: number;
  brief: string;
  status: "running" | "done" | "failed";
  transcript: AgentMessage[];
  error: string | null;
}

/** The noise check after the last round; a deterministic metric skips the repeats. */
export interface HardeningNoise {
  skipped: boolean;
  reason: string;
  scores: number[];
  /** The highest score minus the lowest; null with fewer than two scores. */
  spread: number | null;
}

interface HardeningFixChecks {
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
  /** The lead agent's brief for each session. */
  briefs?: string[];
  sessions?: HardeningSession[];
  /** The user's notes on the fixes they rejected; the next draft reads them. */
  fix_notes?: string[];
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
  /** K, the parallel attacker sessions of each round. */
  parallel_sessions?: number;
  /** The sessions of the round that runs now, for the page to poll. */
  live_sessions?: HardeningSession[];
  /** Null until the noise check after the last round runs. */
  noise?: HardeningNoise | null;
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
  /** K, from 1 to 8; the server uses 3 when it is not given. */
  parallel_sessions?: number;
}

/** Body of POST /hardening/jobs/{id}/fix. A reject with a note sends the round back to drafting. */
export interface HardeningFixDecisionRequest {
  approve: boolean;
  /** At most 4000 characters. */
  note?: string | null;
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
