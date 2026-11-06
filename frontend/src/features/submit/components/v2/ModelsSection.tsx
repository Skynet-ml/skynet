"use client";

import * as React from "react";
import type { BlackboxHarness, ModelConfig } from "@/shared/types/api";
import type { OptimizerTarget, OptimizerChoice } from "@/shared/types/unified-run";
import { resolveOptimizer } from "@/shared/types/unified-run";
import { HarnessLogo } from "@/shared/ui/harness-logo";
import { HelpTip } from "@/shared/ui/help-tip";
import { AddModelButton, ModelChip } from "@/shared/ui/model-chip";
import { NumberInput } from "@/shared/ui/number-input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { TOUCH_FIELD } from "@/shared/ui/touch";
import { BLACKBOX_HARNESSES, harnessLabel } from "@/shared/lib/blackbox-harness";
import { tip } from "@/shared/lib/tooltips";
import { cn } from "@/shared/lib/utils";
import { defaultOpen, useExperienceLevel } from "@/features/experience";
import {
  HostAgentPermissionField,
  HostCliChoice,
  hostCliChoices,
  hostCliPatch,
  hostCliValue,
  isCliProvider,
} from "@/features/local-runtime";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { msg } from "@/shared/lib/messages";

import { emptyModelConfig } from "../../constants";
import { useWizardV2Shell } from "../../hooks/use-unified-wizard";
import { useModelCatalog, useRecentModelConfigs } from "../../hooks/use-submit-wizard-data";
import { DEFAULT_PROPOSER, proposerKnobs, usesNativeProposer } from "../../lib/engine-contract";
import { keepEconomy } from "../../lib/model-config";
import {
  DEFAULT_SHINKA_SETTINGS,
  MAX_EXTRA_OPTIMIZATION_MODELS,
  takesExtraModels,
} from "../../lib/shinka-settings";
import { Disclosure } from "../Disclosure";
import { ModelConfigModal } from "../ModelConfigModal";
import { MaxToolCallsField } from "../blackbox/MaxToolCallsField";
import { ModelRoleRow } from "../blackbox/ModelRoleRow";
import { ShinkaSettingsPanel } from "../blackbox/ShinkaSettingsPanel";
import { Field, StepCard } from "../blackbox/shared";
import { v2Msg } from "./ArtifactConfirmCard";
import { EconomySwitch } from "./EconomySwitch";

const MOBILE_MODEL_CHIP_CLASS =
  "min-h-[44px] max-lg:[&_button]:min-h-[44px] max-lg:[&_button]:min-w-[44px] max-lg:[&_button]:opacity-100";

type Editing = {
  config: ModelConfig;
  onSave: (config: ModelConfig) => void;
  label: string;
  modelDefaultsOnly?: boolean;
};

/**
 * The models step 3 asks for: the task model the run code calls, the
 * reflection model the optimizer thinks with, ShinkaEvolve's extra models,
 * and the proposer settings of the engines that drive a coding agent. The
 * metric model lives with the metric in step 4.
 */
export function ModelsSection({ kind, choice }: { kind: OptimizerTarget; choice: OptimizerChoice }) {
  const { state, patchOptimizer, guided, runSource } = useWizardV2Shell();
  const t = (key: string) => v2Msg(key, guided);
  const level = useExperienceLevel();
  const catalog = useModelCatalog(runSource);
  const { recentConfigs, saveToRecent, removeRecentConfig } = useRecentModelConfigs();
  const [editing, setEditing] = React.useState<Editing | null>(null);
  const [proposerOpen, setProposerOpen] = React.useState(() =>
    defaultOpen("wizard.optimizer_settings", level),
  );
  const [shinkaOpen, setShinkaOpen] = React.useState(() =>
    defaultOpen("wizard.shinka_settings", level),
  );

  const { taskModel, reflectionModel, extraReflectionModels, proposer, shinka } = state.optimizer;
  const resolved = resolveOptimizer(choice, kind);
  const nativeProposer = usesNativeProposer(choice, kind);
  const knobs = proposerKnobs(choice, kind);
  const extraModels = takesExtraModels(choice);
  const showShinka = resolved === "shinka_evolve" || resolved === "auto";
  // DSPy programs call the task model through the harness's configured LM, so
  // they cannot run without one; plain run code may never call `llm`.
  const taskModelRequired = kind === "dspy_program";
  const taskLabel = t("submit.v2.optimizer.models.task.label");
  const reflectionLabel = t("submit.v2.optimizer.models.reflection.label");
  const updateProposer = (patch: Partial<typeof proposer>) =>
    patchOptimizer({ proposer: { ...proposer, ...patch } });

  return (
    <StepCard
      title={t("submit.v2.optimizer.models.title")}
      description={t("submit.v2.optimizer.models.desc")}
    >
      <ModelRoleRow
        id="v2-task-model"
        role={taskLabel}
        binding={taskModelRequired ? undefined : t("submit.v2.optimizer.models.task.optional")}
        description={t(
          kind === "dspy_program"
            ? "submit.v2.optimizer.models.task.desc_dspy"
            : "submit.v2.optimizer.models.task.desc",
        )}
      >
        <ModelChip
          config={taskModel ?? emptyModelConfig()}
          className={MOBILE_MODEL_CHIP_CLASS}
          roleLabel={taskLabel}
          required={taskModelRequired}
          catalogModels={catalog?.models}
          onClick={() =>
            setEditing({
              config: taskModel ?? emptyModelConfig(),
              onSave: (next) =>
                patchOptimizer({
                  taskModel: next.name.trim() ? keepEconomy(next, taskModel) : null,
                }),
              label: taskLabel,
            })
          }
          onRemove={taskModel?.name ? () => patchOptimizer({ taskModel: null }) : undefined}
        />
        {taskModelRequired && !taskModel?.name.trim() && (
          <p role="alert" className="text-xs text-destructive">
            {t("submit.v2.optimizer.models.task.required")}
          </p>
        )}
        <EconomySwitch
          id="v2-task-economy"
          config={taskModel}
          onChange={(economy) =>
            taskModel && patchOptimizer({ taskModel: { ...taskModel, economy_mode: economy } })
          }
        />
      </ModelRoleRow>

      <ModelRoleRow
        id="v2-reflection-model"
        role={reflectionLabel}
        description={[
          t("submit.v2.optimizer.models.reflection.desc"),
          nativeProposer ? t("submit.v2.optimizer.models.reflection.agent_hint") : null,
        ]
          .filter(Boolean)
          .join(" ")}
        tip={tip("blackbox.config.reflection_model")}
      >
        <ModelChip
          config={reflectionModel ?? emptyModelConfig()}
          modelDefaultsOnly={nativeProposer}
          className={MOBILE_MODEL_CHIP_CLASS}
          roleLabel={reflectionLabel}
          required
          catalogModels={catalog?.models}
          onClick={() =>
            setEditing({
              config: reflectionModel ?? emptyModelConfig(),
              onSave: (next) =>
                patchOptimizer({
                  reflectionModel: next.name.trim() ? keepEconomy(next, reflectionModel) : null,
                }),
              label: reflectionLabel,
              modelDefaultsOnly: nativeProposer,
            })
          }
          onRemove={
            reflectionModel?.name ? () => patchOptimizer({ reflectionModel: null }) : undefined
          }
        />
        {extraModels &&
          extraReflectionModels.map((model, index) => (
            <ModelChip
              key={index}
              config={model}
              className={MOBILE_MODEL_CHIP_CLASS}
              roleLabel={reflectionLabel}
              catalogModels={catalog?.models}
              onClick={() =>
                setEditing({
                  config: model,
                  onSave: (next) =>
                    patchOptimizer({
                      extraReflectionModels: extraReflectionModels.map((m, i) =>
                        i === index ? keepEconomy(next, m) : m,
                      ),
                    }),
                  label: reflectionLabel,
                })
              }
              onRemove={() =>
                patchOptimizer({
                  extraReflectionModels: extraReflectionModels.filter((_, i) => i !== index),
                })
              }
            />
          ))}
        {extraModels && extraReflectionModels.length < MAX_EXTRA_OPTIMIZATION_MODELS && (
          <HelpTip text={tip("submit.blackbox.shinka.extra_models")}>
            <AddModelButton
              label={t("submit.v2.optimizer.models.add_extra")}
              onClick={() =>
                setEditing({
                  config: emptyModelConfig(),
                  onSave: (next) => {
                    if (next.name.trim())
                      patchOptimizer({
                        extraReflectionModels: [
                          ...extraReflectionModels,
                          keepEconomy(next, reflectionModel),
                        ],
                      });
                  },
                  label: reflectionLabel,
                })
              }
            />
          </HelpTip>
        )}
        {!reflectionModel?.name.trim() && (
          <p role="alert" className="text-xs text-destructive">
            {t("submit.v2.optimizer.models.reflection.required")}
          </p>
        )}
        <EconomySwitch
          id="v2-reflection-economy"
          config={reflectionModel}
          note={nativeProposer ? msg("submit.blackbox.economy.native_hint") : undefined}
          onChange={(economy) =>
            reflectionModel &&
            patchOptimizer({
              reflectionModel: { ...reflectionModel, economy_mode: economy },
              // ShinkaEvolve's extra models share the reflection role, so they share its choice.
              extraReflectionModels: extraReflectionModels.map((m) => ({
                ...m,
                economy_mode: economy,
              })),
            })
          }
        />
      </ModelRoleRow>

      {nativeProposer && (
        <Disclosure
          id="v2-proposer"
          label={t("submit.v2.optimizer.proposer.title")}
          open={proposerOpen}
          onOpenChange={setProposerOpen}
          trailing={
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <HarnessLogo harness={proposer.harness} size={14} />
              {harnessLabel(proposer.harness)}
            </span>
          }
          alwaysShowTrailing
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <Field
              label={t("submit.v2.optimizer.proposer.harness")}
              htmlFor="v2-proposer-harness"
              tip="submit.blackbox.proposer"
            >
              <Select
                value={proposer.harness}
                onValueChange={(value) => updateProposer({ harness: value as BlackboxHarness })}
              >
                <SelectTrigger id="v2-proposer-harness" className={cn("w-full", TOUCH_FIELD)}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {BLACKBOX_HARNESSES.map((h) => (
                    <SelectItem key={h} value={h}>
                      <span className="flex items-center gap-2">
                        <HarnessLogo harness={h} size={16} />
                        <span>{harnessLabel(h)}</span>
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            {knobs.candidates && (
              <Field
                label={t("submit.v2.optimizer.proposer.candidates")}
                htmlFor="v2-proposer-candidates"
                tip="submit.blackbox.proposer_candidates"
              >
                <NumberInput
                  id="v2-proposer-candidates"
                  value={
                    proposer.max_candidates_per_iter ??
                    DEFAULT_PROPOSER.max_candidates_per_iter ??
                    ""
                  }
                  onChange={(value) => updateProposer({ max_candidates_per_iter: value })}
                  onClear={() =>
                    updateProposer({
                      max_candidates_per_iter: DEFAULT_PROPOSER.max_candidates_per_iter,
                    })
                  }
                  min={1}
                  max={8}
                />
              </Field>
            )}
            <MaxToolCallsField
              id="v2-proposer-tool-calls"
              value={proposer.max_tool_calls}
              onChange={(value) => updateProposer({ max_tool_calls: value })}
            />
            {isLocalMode() && (
              <Field label={msg("host_agent.cli.label")} htmlFor="v2-proposer-host-cli">
                <Select
                  value={hostCliValue(proposer.host_cli)}
                  onValueChange={(value) =>
                    updateProposer(hostCliPatch(value, proposer.host_permission))
                  }
                >
                  <SelectTrigger id="v2-proposer-host-cli" className={cn("w-full", TOUCH_FIELD)}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {hostCliChoices().map(({ value, label }) => (
                      <SelectItem key={value} value={value}>
                        <HostCliChoice value={value} label={label} />
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            )}
            {isLocalMode() && isCliProvider(proposer.host_cli) && (
              <HostAgentPermissionField
                value={proposer.host_permission}
                onChange={(next) => updateProposer({ host_permission: next })}
              />
            )}
          </div>
        </Disclosure>
      )}

      {showShinka && (
        <ShinkaSettingsPanel
          settings={shinka ?? DEFAULT_SHINKA_SETTINGS}
          onChange={(next) => patchOptimizer({ shinka: next })}
          open={shinkaOpen}
          onOpenChange={setShinkaOpen}
          maxToolCalls={proposer.max_tool_calls}
          onMaxToolCallsChange={(value) => updateProposer({ max_tool_calls: value })}
          label={t(
            resolved === "auto"
              ? "submit.v2.optimizer.shinka.title_auto"
              : "submit.v2.optimizer.shinka.title",
          )}
        />
      )}

      {editing && (
        <ModelConfigModal
          open
          onOpenChange={(open) => {
            if (!open) setEditing(null);
          }}
          config={editing.config}
          onSave={(config) => {
            editing.onSave(config);
            if (config.name.trim()) saveToRecent(keepEconomy(config, null));
            setEditing(null);
          }}
          roleLabel={editing.label}
          catalogModels={catalog?.models}
          recentConfigs={recentConfigs}
          onRemoveRecent={removeRecentConfig}
          modelDefaultsOnly={editing.modelDefaultsOnly}
          remote={runSource === "hosted"}
        />
      )}
    </StepCard>
  );
}
