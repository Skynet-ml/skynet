/** Fractional amounts remain decimal strings; only the server admits or settles spending. */
export interface ExecutionBudget {
  id: string;
  total_cents: number;
  revision: number;
  generation: number;
  state: string;
  job_id: string | null;
  setup_spent_cents: string;
  run_spent_cents: string;
  reserved_cents: string;
  available_cents: string;
  billed_cents: number;
  wallet_setup_spent_cents: string;
  wallet_run_spent_cents: string;
  wallet_reserved_cents: number;
  account_available_cents: number;
  external_spent_cents: string;
  pending_operations: number;
  blocked_reason: string | null;
  uncapped: boolean;
  /** Whether the limit also counts setup checks (SKY-925); fixed when the budget is created. */
  setup_in_total: boolean;
}

export interface ExecutionBudgetRef {
  id: string;
  revision: number;
}

/** Job viewers receive run spending without the owner's account-wide balance. */
export type JobExecutionBudget = Omit<ExecutionBudget, "account_available_cents">;
