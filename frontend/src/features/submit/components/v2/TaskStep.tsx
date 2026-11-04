"use client";

import * as React from "react";

import { Brain, GithubLogo } from "@/shared/ui/icons";
import { ExpandableTextarea } from "@/shared/ui/expandable-textarea";
import { PingDot } from "@/shared/ui/ping-dot";
import { Label } from "@/shared/ui/primitives/label";
import { TEXTAREA_SURFACE_CLASS } from "@/shared/ui/primitives/textarea";
import { Segmented } from "@/shared/ui/segmented";
import {
  AgentPanelDock,
  isGeneralistAgentEnabled,
  useAgentFieldLock,
} from "@/features/agent-panel";

import { useWizardV2 } from "../../hooks/use-unified-wizard";
import { emptyRepoDraft, programTargetOptimizer, repoTargetSlices, taskIssue } from "../../lib/wizard-v2";
import { AuthoringShell } from "../steps/AuthoringShell";
import { ArtifactConfirmCard, v2Msg } from "./ArtifactConfirmCard";
import { RepoTargetCard } from "./RepoTargetCard";
import { WizardScreen } from "./WizardScreen";

/**
 * Step 1, Task + Artifact: the setup agent in the side panel writes the
 * objective, background and starting version; the main pane shows them
 * editable. Screen 0 holds the objective and background, screen 1 the
 * artifact.
 */
export function TaskStep({ page }: { page: number }) {
  const { state, guided, patchTask, patchInputs, patchOptimizer } = useWizardV2();
  const task = state.task;
  const t = (key: string) => v2Msg(key, guided);
  const startingLocked = useAgentFieldLock("starting_version");

  const setTarget = (target: "dspy_program" | "repo") => {
    if (target === "repo") {
      patchTask({ repo: task.repo ?? emptyRepoDraft() });
      const { inputs, optimizer } = repoTargetSlices(state.optimizer.choice);
      patchInputs(inputs);
      if (optimizer) patchOptimizer(optimizer);
    } else {
      patchTask({ repo: null, artifactKind: "dspy_program" });
      const optimizer = programTargetOptimizer(state.optimizer.choice);
      if (optimizer) patchOptimizer(optimizer);
    }
  };

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
        <Segmented<"dspy_program" | "repo">
          label={t("submit.v2.repo.target_label")}
          value={task.repo ? "repo" : "dspy_program"}
          onChange={setTarget}
          className="w-full"
          options={[
            {
              value: "dspy_program",
              label: t("submit.v2.task.artifact.kind.dspy_program"),
              tip: t("submit.v2.task.artifact.kind.dspy_program.desc"),
              icon: <Brain className="size-3.5" aria-hidden="true" />,
            },
            {
              value: "repo",
              label: t("submit.blackbox.repo.kind_repo"),
              tip: t("submit.blackbox.repo.kind_repo_desc"),
              icon: <GithubLogo className="size-3.5" aria-hidden="true" />,
            },
          ]}
        />
        {task.repo ? (
          <RepoTargetCard
            repo={task.repo}
            onChange={(repo) => patchTask({ repo })}
            issue={taskIssue(task)}
            guided={guided}
          />
        ) : (
          <ArtifactConfirmCard
            kind={task.artifactKind}
            startingVersion={task.startingVersion}
            onStartingVersionChange={(startingVersion) => patchTask({ startingVersion })}
            rationale={task.artifactRationale}
            issue={taskIssue(task)}
            writing={startingLocked}
            guided={guided}
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
