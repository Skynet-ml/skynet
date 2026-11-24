import type { OptimizationSummaryResponse } from "@/shared/types/api";
import type { JobExecutionBudget } from "@/shared/types/execution-budget";

type LimitLedger = Pick<JobExecutionBudget, "job_id" | "run_spent_cents" | "reserved_cents">;

export function isBudgetStop(
  job: Pick<OptimizationSummaryResponse, "status" | "stop_reason">,
): boolean {
  return job.status === "stopped" && job.stop_reason === "budget_reached";
}

/** The worker parked the run at a checkpoint because its measured burn outgrows the limit. */
export function isBudgetPause(
  job: Pick<OptimizationSummaryResponse, "status" | "stop_reason">,
): boolean {
  return job.status === "paused" && job.stop_reason === "budget_projected";
}

/** Only the worker's completed selection establishes an evaluated result. */
export function budgetResultKind(
  job: Pick<OptimizationSummaryResponse, "result_availability" | "terminal_evidence">,
): "seed" | "evaluated" | "none" {
  if (job.result_availability !== "evaluated") return "none";
  return job.terminal_evidence?.candidate_origin === "seed" ? "seed" : "evaluated";
}

/**
 * What a run's budget block shows against its limit. Setup checks bill the
 * balance, not the limit (SKY-925), so only run_spent_cents counts. The bar
 * adds the run's holds once a job owns the budget, as the server does.
 */
export function limitPaceCents(budget: LimitLedger): { spentCents: number; usedCents: number } {
  const spentCents = Number(budget.run_spent_cents);
  const usedCents = budget.job_id ? spentCents + Number(budget.reserved_cents) : spentCents;
  return { spentCents, usedCents };
}

/**
 * The smallest whole-cent limit above what the run already committed to its
 * limit. Setup spend that billed the balance is left out (SKY-925).
 */
export function committedLimitFloorCents(budget: LimitLedger): number {
  return Math.floor(Number(budget.run_spent_cents) + Number(budget.reserved_cents)) + 1;
}
