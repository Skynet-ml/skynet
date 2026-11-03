/**
 * What blocks each wizard step, and so what blocks Run. Pure, so the gate the
 * wizard enforces is the gate the tests check, at every experience level.
 */
import type { ModelConfig } from "@/shared/types/api";
import {
  optimizersFor,
  type ContractCheckItem,
  type OptimizerCatalogResponse,
} from "@/shared/types/unified-run";

import { inputsIssue } from "./inputs-mode";
import {
  WIZARD_V2_STEPS,
  contractSnapshot,
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
    catalog.artifact_kind !== state.task.artifactKind ||
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
      // A repository run is scored on its checkout and takes no inputs.
      if (task.repo) return null;
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
      const kind = task.artifactKind;
      if (!kind || !inputs.mode) return "submit.v2.optimizer.needs_setup";
      // Auto has no repository strategy; every named engine edits a checkout.
      if (task.repo && optimizer.choice === "auto") return "submit.v2.optimizer.issue.choice";
      if (!optimizersFor(kind, inputs.mode).includes(optimizer.choice))
        return "submit.v2.optimizer.issue.choice";
      const option = catalogOption(state, catalog);
      if (option === null) return "submit.v2.optimizer.issue.choice";
      if (option && !option.available) return "submit.v2.optimizer.issue.unavailable";
      if (kind === "dspy_program" && !hasModel(optimizer.taskModel))
        return "submit.v2.optimizer.models.task.required";
      if (!hasModel(optimizer.reflectionModel))
        return "submit.v2.optimizer.models.reflection.required";
      return null;
    }
    case "metric": {
      // A repository scorer is checked on the server, against the staged checkout, at submit.
      if (task.repo)
        return workspace.metricCode.trim() ? null : "submit.v2.metric.issue.metric_code";
      if (!workspace.runCode.trim()) return "submit.v2.metric.issue.run_code";
      if (!workspace.metricCode.trim()) return "submit.v2.metric.issue.metric_code";
      if (!workspace.lastCheck) return "submit.v2.metric.check.needed";
      if (workspace.lastCheckSnapshot !== contractSnapshot(state))
        return "submit.v2.metric.check.stale";
      return workspace.lastCheck.ok ? null : "submit.v2.metric.check.failed";
    }
  }
}

// The reason sits under the Run button, so it stays one line however long
// the item's error is; the full traceback lives in the check panel.
const REASON_MAX_CHARS = 120;

function oneLine(text: string): string {
  const line = text.split("\n").find((part) => part.trim())?.trim() ?? "";
  return line.length > REASON_MAX_CHARS ? `${line.slice(0, REASON_MAX_CHARS - 1)}…` : line;
}

/**
 * Why Run is blocked, or null when it may run. A failed validity check reports
 * its first failing item's localized message on one line, so the person sees
 * what to fix.
 * `message` renders a check item in the active locale (injected so this stays
 * pure); while `agentWriting`, missing code reads as being written, not as a
 * job left to the person.
 */
export function runBlockedReason(
  state: WizardV2State,
  catalog: OptimizerCatalogResponse | null,
  {
    message = (check) => check.message,
    agentWriting = false,
  }: { message?: (check: ContractCheckItem) => string; agentWriting?: boolean } = {},
): string | null {
  for (const step of WIZARD_V2_STEPS) {
    const issue = computeStepIssue(state, step, catalog);
    if (!issue) continue;
    if (agentWriting && issue === "submit.v2.metric.issue.run_code")
      return "submit.v2.metric.agent.writing_run";
    if (agentWriting && issue === "submit.v2.metric.issue.metric_code")
      return "submit.v2.metric.agent.writing_metric";
    if (issue === "submit.v2.metric.check.failed") {
      const failed = state.workspace.lastCheck?.checks.find((item) => item.status === "failed");
      return failed ? oneLine(message(failed)) || issue : issue;
    }
    return issue;
  }
  return null;
}
