/**
 * What blocks each wizard step, and so what blocks Run. Pure, so the gate the
 * wizard enforces is the gate the tests check, at every experience level.
 */
import type { ModelConfig } from "@/shared/types/api";
import { optimizersFor, type OptimizerCatalogResponse } from "@/shared/types/unified-run";

import { inputsIssue } from "./inputs-mode";
import {
  WIZARD_V2_STEPS,
  taskIssue,
  type WizardStepId,
  type WizardV2State,
} from "./wizard-v2";

export const hasModel = (config: ModelConfig | null | undefined): config is ModelConfig =>
  Boolean(config?.name.trim());

/** The catalog entry for the current choice, when the catalog matches the setup. */
function catalogOption(state: WizardV2State, catalog: OptimizerCatalogResponse | null) {
  if (
    !catalog ||
    catalog.artifact_kind !== "repo" ||
    catalog.data_mode !== state.inputs.mode
  )
    return undefined;
  return catalog.options.find((option) => option.id === state.optimizer.choice) ?? null;
}

/** The message key (or exact text) of what blocks one step; null when it is complete. */
export function computeStepIssue(
  state: WizardV2State,
  step: WizardStepId,
  catalog: OptimizerCatalogResponse | null,
): string | null {
  const { task, inputs, optimizer, workspace } = state;
  switch (step) {
    case "task":
      return taskIssue(task);
    case "inputs": {
      // A repository run takes inputs only when the interview found it needs them.
      if (!task.needsData) return null;
      const caseCount =
        inputs.cases.length > 0 ? inputs.cases.length : inputs.stagedDatasetId ? inputs.rowCount : 0;
      return inputsIssue({
        mode: inputs.mode,
        caseCount,
        inputFields: inputs.inputFields,
        expectedField: inputs.expectedField,
        split: inputs.split,
      });
    }
    case "optimizer": {
      if (!inputs.mode) return "submit.v2.optimizer.needs_setup";
      if (!optimizersFor().includes(optimizer.choice))
        return "submit.v2.optimizer.issue.choice";
      const option = catalogOption(state, catalog);
      if (option === null) return "submit.v2.optimizer.issue.choice";
      if (option && !option.available) return "submit.v2.optimizer.issue.unavailable";
      if (!hasModel(optimizer.reflectionModel))
        return "submit.v2.optimizer.models.reflection.required";
      return null;
    }
    case "metric": {
      // A repository scorer is checked on the server, against the staged checkout, at submit.
      return workspace.metricCode.trim() ? null : "submit.v2.metric.issue.metric_code";
    }
  }
}

/**
 * Why Run is blocked, or null when it may run. While `agentWriting`, missing
 * code reads as being written, not as a job left to the person.
 */
export function runBlockedReason(
  state: WizardV2State,
  catalog: OptimizerCatalogResponse | null,
  { agentWriting = false }: { agentWriting?: boolean } = {},
): string | null {
  for (const step of WIZARD_V2_STEPS) {
    const issue = computeStepIssue(state, step, catalog);
    if (!issue) continue;
    if (agentWriting && issue === "submit.v2.metric.issue.metric_code")
      return "submit.v2.metric.agent.writing_metric";
    return issue;
  }
  return null;
}
