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
import { takeLandingGoal } from "@/features/landing";

import { useWizardV2, useWizardV2ShellOptional } from "../../hooks/use-unified-wizard";
import { taskIssue } from "../../lib/wizard-v2";
import { AuthoringShell } from "../steps/AuthoringShell";
import { v2Msg } from "./ArtifactConfirmCard";
import { RepoTargetCard } from "./RepoTargetCard";
import { RunPlaceCard } from "./RunPlaceCard";
import { WizardScreen } from "./WizardScreen";

/**
 * Step 1, Task + Artifact: the setup agent in the side panel writes the
 * objective and background; the main pane shows them editable. Screen 0
 * holds the objective and background, screen 1 the repository. Every run
 * optimizes a repository.
 */
export function TaskStep({ page }: { page: number }) {
  const { state, guided, patchTask } = useWizardV2();
  // A folder on this computer can only run here, never on the web (SKY-993).
  const runsHere = (useWizardV2ShellOptional()?.runOn ?? "local") === "local";
  const task = state.task;
  const t = (key: string) => v2Msg(key, guided);

  // A goal written on the signed-out landing page opens the setup (SKY-922).
  // It is taken once, so a later setup does not get it again.
  React.useEffect(() => {
    const goal = takeLandingGoal();
    if (goal && !task.objective.trim()) patchTask({ objective: goal });
    // Runs once, on the first render of the step.
  }, []);

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
        <RunPlaceCard />
        <TaskField
          id="v2-objective"
          field="task_objective"
          label={t("submit.v2.task.objective_label")}
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
        <RepoTargetCard
          repo={task.repo}
          onChange={(repo) => patchTask({ repo })}
          issue={taskIssue(task)}
          guided={guided}
          localFolders={runsHere}
          seed={{
            objective: task.objective,
            scorerCode: state.workspace.metricCode,
            cases: state.inputs.cases,
          }}
        />
      </WizardScreen>
    </AuthoringShell>
  );
}

function TaskField({
  id,
  field,
  label,
  placeholder,
  value,
  onChange,
  rows,
}: {
  id: string;
  field: Parameters<typeof useAgentFieldLock>[0];
  label: string;
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
          {textarea}
        </div>
      )}
    </ExpandableTextarea>
  );
}
