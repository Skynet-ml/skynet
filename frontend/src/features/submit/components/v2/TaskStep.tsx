"use client";

import * as React from "react";

import { ExpandableTextarea } from "@/shared/ui/expandable-textarea";
import { PingDot } from "@/shared/ui/ping-dot";
import { Label } from "@/shared/ui/primitives/label";
import { TEXTAREA_SURFACE_CLASS } from "@/shared/ui/primitives/textarea";
import {
  AgentPanelDock,
  isGeneralistAgentEnabled,
  useAgentFieldLock,
} from "@/features/agent-panel";

import { useWizardV2 } from "../../hooks/use-unified-wizard";
import { targetPatch, taskIssue } from "../../lib/wizard-v2";
import { AuthoringShell } from "../steps/AuthoringShell";
import { v2Msg } from "./ArtifactConfirmCard";
import { RepoTargetCard } from "./RepoTargetCard";
import { WizardScreen } from "./WizardScreen";

/**
 * Step 1, Task + Artifact: the setup agent in the side panel writes the
 * objective and background; the main pane shows them editable. Screen 0
 * holds the objective and background, screen 1 the repository. Every run
 * optimizes a repository.
 */
export function TaskStep({ page }: { page: number }) {
  const { state, guided, patchTask, patchInputs, patchOptimizer } = useWizardV2();
  const task = state.task;
  const t = (key: string) => v2Msg(key, guided);

  // A fresh wizard and an older draft both start without a repository target;
  // move them onto one so the repository card and the repo optimizers apply.
  React.useEffect(() => {
    if (task.repo) return;
    const patch = targetPatch("repo", null, state.optimizer.choice);
    patchTask(patch.task);
    if (patch.inputs) patchInputs(patch.inputs);
    if (patch.optimizer) patchOptimizer(patch.optimizer);
  }, [task.repo]);

  return (
    <AuthoringShell
      title={t("submit.v2.task.title")}
      description={t("submit.v2.task.desc")}
      sidePanel={
        isGeneralistAgentEnabled() && state.step === "task" ? (
          <AgentPanelDock className="absolute inset-0" />
        ) : null
      }
    >
      <WizardScreen
        active={page === 0}
        className="relative flex flex-col gap-5 px-4 py-5 sm:px-6 sm:py-6"
      >
        <TaskField
          id="v2-objective"
          field="task_objective"
          label={t("submit.v2.task.objective_label")}
          hint={t("submit.v2.task.objective_hint_auto")}
          placeholder={t("submit.v2.task.objective_placeholder")}
          value={task.objective}
          onChange={(objective) => patchTask({ objective })}
          rows={3}
        />
        <TaskField
          id="v2-background"
          field="task_background"
          label={t("submit.v2.task.background_label")}
          placeholder={t("submit.v2.task.background_placeholder")}
          value={task.background}
          onChange={(background) => patchTask({ background })}
          rows={4}
        />
      </WizardScreen>
      <WizardScreen
        active={page === 1}
        className="relative flex flex-col gap-5 px-4 py-5 sm:px-6 sm:py-6"
      >
        {task.repo && (
          <RepoTargetCard
            repo={task.repo}
            onChange={(repo) => patchTask({ repo })}
            issue={taskIssue(task)}
            guided={guided}
            seed={{
              objective: task.objective,
              scorerCode: state.workspace.metricCode,
              cases: state.inputs.cases,
            }}
          />
        )}
      </WizardScreen>
    </AuthoringShell>
  );
}

function TaskField({
  id,
  field,
  label,
  hint,
  placeholder,
  value,
  onChange,
  rows,
}: {
  id: string;
  field: Parameters<typeof useAgentFieldLock>[0];
  label: string;
  hint?: string;
  placeholder: string;
  value: string;
  onChange: (value: string) => void;
  rows: number;
}) {
  const locked = useAgentFieldLock(field);
  return (
    <ExpandableTextarea
      id={id}
      label={label}
      value={value}
      onChange={onChange}
      placeholder={placeholder}
      rows={rows}
      readOnly={locked}
      className={TEXTAREA_SURFACE_CLASS}
    >
      {({ textarea, trigger }) => (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Label htmlFor={id}>{label}</Label>
              {locked && <PingDot size="sm" tone="agent" />}
            </div>
            {trigger}
          </div>
          {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
          {textarea}
        </div>
      )}
    </ExpandableTextarea>
  );
}
