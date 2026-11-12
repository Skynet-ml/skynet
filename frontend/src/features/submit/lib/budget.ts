/**
 * Budget arithmetic and the run-name suggestion for the submission wizard.
 *
 * One total budget covers setup checks and the optimization itself. Until the
 * server-owned accounting record exists, run spend and reservations are zero
 * and the setup spend is what the dry-run responses reported.
 */
export interface BudgetLedger {
  total: number | null;
  setupSpent: number;
  runSpent: number;
  reserved: number;
}

/** Cents still available under the total, or null when no total is set. */
export function availableBudget(ledger: BudgetLedger): number | null {
  if (ledger.total == null) return null;
  return Math.max(0, ledger.total - ledger.setupSpent - ledger.runSpent - ledger.reserved);
}

/**
 * A run name from the objective's first sentence, without a paid call. Empty
 * when the objective is blank; the caller keeps any name the user typed.
 */
export function suggestedRunName(objective: string, maxChars = 60): string {
  const firstLine = objective.trim().split(/\r?\n/)[0] ?? "";
  const sentence = /^[^.!?]*[.!?]?/.exec(firstLine)?.[0] ?? firstLine;
  const compact = sentence
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[.!?:;,]+$/u, "");
  if (compact.length <= maxChars) return compact;
  const cut = compact.slice(0, maxChars);
  const lastSpace = cut.lastIndexOf(" ");
  return `${(lastSpace > maxChars / 2 ? cut.slice(0, lastSpace) : cut).trimEnd()}…`;
}
