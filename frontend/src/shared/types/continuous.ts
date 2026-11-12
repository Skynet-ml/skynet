/**
 * Continuous optimization wire types (`/optimizations/{id}/continuous`,
 * `/continuous/{planId}/…`). A plan checks a successful run on a schedule
 * and starts a new run only when a better model came out for a model it may
 * switch.
 */

export type ScheduleFrequency = "hourly" | "daily" | "weekly" | "monthly";

/**
 * When a plan checks: every `every` hours/days/weeks/months at `time` (HH:MM)
 * in `timezone`. Weekly rules use `weekdays` (Monday = 0); monthly rules use
 * `month_day`, which falls on the last day of shorter months. Hourly rules use
 * the minute of `time`, counting hours from its hour.
 */
export interface ScheduleRule {
  frequency: ScheduleFrequency;
  every: number;
  time: string;
  weekdays: number[];
  month_day: number;
  timezone: string;
  /** Single checks turned off, as local `YYYY-MM-DDTHH:MM` slots. */
  skip?: string[];
  /** Single checks added outside the repeat, as local `YYYY-MM-DDTHH:MM` slots. */
  extra?: string[];
}

export type ContinuousCycleStatus =
  | "checking"
  | "skipped"
  | "failed"
  | "paused"
  | "optimizing"
  | "regressed"
  | "awaiting_review"
  | "approved"
  | "rejected";

/** The models a plan may switch to a better one (rated higher, priced no higher). */
export interface ContinuousPermitted {
  task_model: boolean;
  reflection_model: boolean;
}

export interface ContinuousCycle {
  id: string;
  slot: string;
  month: string;
  status: ContinuousCycleStatus;
  changes: string[];
  detail_code: string | null;
  detail_params: Record<string, unknown>;
  snapshot: Record<string, unknown> | null;
  optimization_id: string | null;
  incumbent_score: number | null;
  candidate_score: number | null;
  spent_cents: number | null;
  created_at: string;
}

export interface ContinuousUsage {
  month: string;
  /** Null when the plan has no monthly limit; only the wallet gates a run. */
  limit_cents: number | null;
  committed_cents: number;
  resets_at: string;
}

export interface ContinuousPlan {
  id: string;
  source_optimization_id: string;
  incumbent_optimization_id: string;
  /**
   * `waiting` until the run it was set up with succeeds; `ended` when that run
   * failed, was cancelled or is gone, so the schedule never started.
   */
  status: "active" | "paused" | "waiting" | "ended";
  /**
   * While paused: `user` (the owner paused it), `allowance` (monthly limit
   * spent), `funds` (wallet too low) or `failing` (checks failed in a row).
   * While ended: the first run's status (`failed`, `cancelled`, `stopped`), `missing`,
   * or `no_test_split` (the run kept no test cases apart).
   */
  pause_reason:
    | "user"
    | "allowance"
    | "funds"
    | "failing"
    | "failed"
    | "cancelled"
    | "stopped"
    | "missing"
    | "no_test_split"
    | null;
  schedule: ScheduleRule;
  next_check_at: string | null;
  /** The next few check times, earliest first; empty while paused. */
  upcoming_checks: string[];
  permitted: ContinuousPermitted;
  cycle_budget_cents: number;
  usage: ContinuousUsage;
  cycles: ContinuousCycle[];
}

export interface ContinuousState {
  plan: ContinuousPlan | null;
  eligible: boolean;
  /** False when the run kept no test cases apart, so no plan can start from it. */
  held_out?: boolean;
}

export interface ContinuousEnableRequest {
  schedule: ScheduleRule;
  permitted: ContinuousPermitted;
}
