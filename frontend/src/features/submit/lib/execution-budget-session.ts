import type {
  ExecutionBudget,
  ExecutionBudgetRef,
} from "../../../shared/types/execution-budget.ts";

/** Shared durable metadata; workflow snapshots never own separate spending pools. */
export interface WizardBudgetDraft {
  executionBudgetRef?: ExecutionBudgetRef;
  budgetTotalCents?: number | null;
  budgetUncapped?: boolean;
  budgetCreateIdempotencyKey?: string;
  budgetCreateTotalCents?: number;
  budgetCreateUncapped?: boolean;
  submissionIdempotencyKey?: string;
  submissionFingerprint?: string;
}

export interface BudgetSessionDependencies {
  persist: (draft: WizardBudgetDraft) => Promise<void>;
  create: (
    total: number,
    key: string,
    signal: AbortSignal,
    uncapped: boolean,
  ) => Promise<ExecutionBudget>;
  get: (id: string, signal: AbortSignal) => Promise<ExecutionBudget>;
  update: (
    id: string,
    total: number,
    revision: number,
    signal: AbortSignal,
    uncapped: boolean,
  ) => Promise<ExecutionBudget>;
  changed: () => void;
  newKey?: () => string;
}

// The server keeps a total on every budget; a run without a limit never reads it.
const UNCAPPED_PLACEHOLDER_TOTAL = 1;

function errorNumber(error: unknown, key: string): number | null {
  if (!error || typeof error !== "object" || !("params" in error)) return null;
  const params = error.params;
  if (!params || typeof params !== "object" || !(key in params)) return null;
  const value = params[key as keyof typeof params];
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/** Retain safe shared fields when accepting an older or partially written draft. */
export function readBudgetDraft(raw: WizardBudgetDraft): WizardBudgetDraft {
  const ref = raw.executionBudgetRef;
  return {
    ...(ref && typeof ref.id === "string" && Number.isInteger(ref.revision)
      ? { executionBudgetRef: { id: ref.id, revision: ref.revision } }
      : {}),
    ...(raw.budgetTotalCents === null ||
    (Number.isInteger(raw.budgetTotalCents) && (raw.budgetTotalCents ?? 0) > 0)
      ? { budgetTotalCents: raw.budgetTotalCents }
      : {}),
    ...(Number.isInteger(raw.budgetCreateTotalCents) && (raw.budgetCreateTotalCents ?? 0) > 0
      ? { budgetCreateTotalCents: raw.budgetCreateTotalCents }
      : {}),
    ...Object.fromEntries(
      (["budgetUncapped", "budgetCreateUncapped"] as const)
        .filter((key) => typeof raw[key] === "boolean")
        .map((key) => [key, raw[key]]),
    ),
    ...Object.fromEntries(
      (["budgetCreateIdempotencyKey", "submissionIdempotencyKey", "submissionFingerprint"] as const)
        .filter((key) => typeof raw[key] === "string" && raw[key])
        .map((key) => [key, raw[key]]),
    ),
  };
}

/** Serialize one draft's server budget work and fence completions after detachment. */
export class ExecutionBudgetSession {
  draft: WizardBudgetDraft;
  budget: ExecutionBudget | null = null;
  error: string | null = null;
  minimumTotalCents: number | null = null;
  busy = false;
  persistenceUnavailable = false;
  private active = true;
  private controller = new AbortController();
  private pending: Promise<ExecutionBudget> | null = null;
  private readonly newKey: () => string;

  private readonly deps: BudgetSessionDependencies;

  constructor(draft: WizardBudgetDraft, deps: BudgetSessionDependencies) {
    this.deps = deps;
    this.draft = readBudgetDraft(draft);
    this.newKey = deps.newKey ?? (() => crypto.randomUUID());
  }

  setTotal(total: number | null): void {
    if (!this.active || this.draft.budgetTotalCents === total) return;
    this.draft = { ...this.draft, budgetTotalCents: total };
    this.error = null;
    this.minimumTotalCents = null;
    this.deps.changed();
    void this.save().catch((error: unknown) => this.report(error));
  }

  setUncapped(uncapped: boolean): void {
    if (!this.active || (this.draft.budgetUncapped ?? false) === uncapped) return;
    this.draft = { ...this.draft, budgetUncapped: uncapped };
    this.error = null;
    this.minimumTotalCents = null;
    this.deps.changed();
    void this.save().catch((error: unknown) => this.report(error));
  }

  detach(): void {
    this.active = false;
    this.controller.abort();
  }

  async refresh(): Promise<ExecutionBudget | null> {
    if (!this.draft.executionBudgetRef) return null;
    return this.perform(async () => {
      const budget = await this.deps.get(this.draft.executionBudgetRef!.id, this.controller.signal);
      await this.adopt(budget);
      return budget;
    });
  }

  /** Save the create identity before requesting when storage is available; retries always reuse it in memory. */
  async ensure(): Promise<ExecutionBudget> {
    if (this.pending) await this.pending;
    return this.perform(async () => {
      const uncapped = this.draft.budgetUncapped ?? false;
      const total = this.draft.budgetTotalCents;
      if (!uncapped && (!Number.isInteger(total) || total == null || total < 1))
        throw new Error("budget.invalid");
      // A budget without a limit still carries a total; the server ignores it.
      const wanted = uncapped ? (total ?? UNCAPPED_PLACEHOLDER_TOTAL) : total!;
      let budget: ExecutionBudget;
      if (this.draft.executionBudgetRef) {
        budget = await this.deps.get(this.draft.executionBudgetRef.id, this.controller.signal);
      } else {
        if (!this.draft.budgetCreateIdempotencyKey) {
          this.draft = {
            ...this.draft,
            budgetCreateIdempotencyKey: this.newKey(),
            budgetCreateTotalCents: wanted,
            budgetCreateUncapped: uncapped,
          };
        }
        await this.save();
        this.assertActive();
        budget = await this.deps.create(
          this.draft.budgetCreateTotalCents ?? wanted,
          this.draft.budgetCreateIdempotencyKey!,
          this.controller.signal,
          this.draft.budgetCreateUncapped ?? false,
        );
      }
      await this.adopt(budget);
      if (budget.uncapped !== uncapped || (!uncapped && budget.total_cents !== wanted)) {
        try {
          budget = await this.deps.update(
            budget.id,
            uncapped ? budget.total_cents : wanted,
            budget.revision,
            this.controller.signal,
            uncapped,
          );
        } catch (error) {
          await this.restoreRejectedTotal(error, budget);
          throw error;
        }
        await this.adopt(budget);
      }
      return budget;
    });
  }

  /** A server fingerprint identifies the logical submission without persisting credentials. */
  async submissionKey(fingerprint: string): Promise<string> {
    this.assertActive();
    if (this.draft.submissionFingerprint !== fingerprint || !this.draft.submissionIdempotencyKey) {
      this.draft = {
        ...this.draft,
        submissionFingerprint: fingerprint,
        submissionIdempotencyKey: this.newKey(),
      };
    }
    await this.save();
    this.assertActive();
    return this.draft.submissionIdempotencyKey!;
  }

  async adopt(budget: ExecutionBudget): Promise<void> {
    this.assertActive();
    this.budget = budget;
    this.draft = {
      ...this.draft,
      executionBudgetRef: { id: budget.id, revision: budget.revision },
    };
    await this.save();
    this.assertActive();
    this.deps.changed();
  }

  private assertActive(): void {
    if (!this.active) throw new DOMException("Budget session detached", "AbortError");
  }

  private async save(): Promise<void> {
    this.assertActive();
    try {
      await this.deps.persist(this.draft);
      this.persistenceUnavailable = false;
    } catch (error) {
      this.assertActive();
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      this.persistenceUnavailable = true;
      this.deps.changed();
    }
  }

  private report(error: unknown): void {
    if (!this.active) return;
    this.minimumTotalCents = errorNumber(error, "minimum_total_cents");
    this.error = error instanceof Error ? error.message : "budget.invalid";
    this.deps.changed();
  }

  private async restoreRejectedTotal(error: unknown, lastAccepted: ExecutionBudget): Promise<void> {
    const current = errorNumber(error, "current_total_cents");
    if (current == null) return;
    let accepted = lastAccepted;
    try {
      accepted = await this.deps.get(lastAccepted.id, this.controller.signal);
      await this.adopt(accepted);
    } catch (refreshError) {
      this.assertActive();
      if (refreshError instanceof DOMException && refreshError.name === "AbortError")
        throw refreshError;
    }
    this.draft = {
      ...this.draft,
      budgetTotalCents: accepted.total_cents === current ? accepted.total_cents : current,
    };
    await this.save();
    this.assertActive();
    this.deps.changed();
  }

  private perform(operation: () => Promise<ExecutionBudget>): Promise<ExecutionBudget> {
    if (this.pending) return this.pending;
    this.assertActive();
    this.busy = true;
    this.error = null;
    this.deps.changed();
    this.pending = operation()
      .catch((error: unknown) => {
        this.report(error);
        throw error;
      })
      .finally(() => {
        this.pending = null;
        this.busy = false;
        if (this.active) this.deps.changed();
      });
    return this.pending;
  }
}
