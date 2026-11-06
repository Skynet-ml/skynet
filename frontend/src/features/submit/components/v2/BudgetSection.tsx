"use client";

import type { ModelConfig } from "@/shared/types/api";
import type { OptimizerTarget, OptimizerChoice } from "@/shared/types/unified-run";
import { NumberInput } from "@/shared/ui/number-input";

import { useWizardV2 } from "../../hooks/use-unified-wizard";
import { aggregateTokenSource } from "../../lib/run-floor";
import { minScorerRuns, supportsIterationLimit } from "../../lib/engine-contract";
import { TotalBudgetCard } from "../TotalBudgetCard";
import { Field, StepCard } from "../blackbox/shared";
import { v2Msg } from "./ArtifactConfirmCard";

/**
 * Step 3's budget: how many scorer runs the optimizer may spend, the
 * iteration cap where the engine honours one, the score that ends the run
 * early, and the spending limit with the least it can be.
 */
export function BudgetSection({ kind, choice }: { kind: OptimizerTarget; choice: OptimizerChoice }) {
  const ctx = useWizardV2();
  const { state, patchOptimizer, guided } = ctx;
  const t = (key: string, params?: Record<string, string>) => v2Msg(key, guided, params);
  const { budget, taskModel, reflectionModel, extraReflectionModels } = state.optimizer;
  const minRuns = minScorerRuns(choice, kind);
  const iterationLimit = supportsIterationLimit(choice, kind);
  const patchBudget = (patch: Partial<typeof budget>) =>
    patchOptimizer({ budget: { ...budget, ...patch } });
  const models = [taskModel, reflectionModel, ...extraReflectionModels].filter(
    (model): model is ModelConfig => model != null,
  );

  return (
    <StepCard
      title={t("submit.v2.optimizer.budget.title")}
      description={t("submit.v2.optimizer.budget.desc")}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label={t("submit.v2.optimizer.budget.max_runs")}
          htmlFor="v2-max-runs"
          tip="blackbox.config.budget_runs"
        >
          <NumberInput
            id="v2-max-runs"
            value={budget.max_scorer_runs}
            onChange={(value) => patchBudget({ max_scorer_runs: value })}
            min={minRuns}
            max={100000}
            step={10}
          />
          {budget.max_scorer_runs < minRuns && (
            <p className="text-xs text-[var(--warning)]" role="status">
              {t("submit.v2.optimizer.budget.min_runs", { p1: String(minRuns) })}
            </p>
          )}
        </Field>
        <Field
          label={t("submit.v2.optimizer.budget.stop_at")}
          htmlFor="v2-stop-at"
          tip="blackbox.config.budget_stop"
        >
          <NumberInput
            id="v2-stop-at"
            value={budget.stop_at_score ?? ""}
            onChange={(value) => patchBudget({ stop_at_score: value })}
            onClear={() => patchBudget({ stop_at_score: null })}
            min={0}
            step={0.1}
          />
        </Field>
        {iterationLimit && (
          <Field
            label={t("submit.v2.optimizer.budget.max_iterations")}
            htmlFor="v2-max-iterations"
            tip="blackbox.config.budget_iterations"
          >
            <NumberInput
              id="v2-max-iterations"
              value={budget.max_iterations ?? ""}
              onChange={(value) => patchBudget({ max_iterations: value })}
              onClear={() => patchBudget({ max_iterations: null })}
              min={1}
              max={1000}
            />
          </Field>
        )}
      </div>

      <TotalBudgetCard w={ctx} mode={aggregateTokenSource(models)} />
    </StepCard>
  );
}
