"use client";

import { useState } from "react";

import { useSettingsModal } from "@/features/settings";
import { Warning } from "@/shared/ui/icons";

import { useWizardV2 } from "../../hooks/use-unified-wizard";
import { stepPages } from "../../lib/wizard-pages";
import { optimizerModelsReady, optimizerTarget } from "../../lib/wizard-v2";
import { AgentFieldLock } from "./AgentFieldLock";
import { v2Msg } from "./ArtifactConfirmCard";
import { BudgetSection } from "./BudgetSection";
import { GeneralistAgentShell } from "./GeneralistAgentShell";
import { ModelsSection } from "./ModelsSection";
import { OptimizerPicker } from "./OptimizerPicker";
import { WizardScreen } from "./WizardScreen";

// The wizard fields the setup agent writes on each section.
const OPTIMIZER_FIELDS = ["optimizer_choice"] as const;
const MODEL_FIELDS = ["model_config", "reflection_model_config"] as const;
const BUDGET_FIELDS = ["max_cost_cents"] as const;

/**
 * Step 3, Optimizer: which optimizer runs (Auto unless the user picks
 * another), the models it uses and what it may spend. Expert gets the three
 * sections in that order, one screen each, since the models and the budget
 * both follow from the chosen optimizer. Standard gets one screen: the engine
 * in plain words with its estimated cost, then the budget; the models come
 * from the Settings defaults and only show when there are none to use.
 */
export function OptimizerStep({ page }: { page: number }) {
  const { state, guided } = useWizardV2();
  const { openTo } = useSettingsModal();
  const t = (key: string) => v2Msg(key, guided);
  const kind = optimizerTarget(state.task);
  const mode = state.inputs.mode;
  const choice = state.optimizer.choice;
  const pages = stepPages("optimizer", state);
  const shown = pages[Math.min(page, pages.length - 1)];
  // Once shown, the models stay on Standard's screen, so picking the last one
  // doesn't pull the section out from under the user.
  const [askModels, setAskModels] = useState(false);
  if (!askModels && kind && mode && !optimizerModelsReady(state)) setAskModels(true);

  return (
    <GeneralistAgentShell active={state.step === "optimizer"}>
      <div className="flex min-h-0 flex-1 flex-col gap-5 px-4 py-5 sm:px-6 sm:py-6">
        <header className="flex shrink-0 flex-col gap-1">
          <h3 className="text-base font-semibold text-foreground">
            {t("submit.v2.optimizer.title")}
          </h3>
          <p className="text-sm text-muted-foreground">
            {t(shown === "plan" ? "submit.v2.optimizer.plan.desc" : "submit.v2.optimizer.desc")}
            {shown === "plan" && (
              <>
                {" "}
                <button
                  type="button"
                  onClick={() => openTo("wizard")}
                  className="rounded-sm font-medium text-foreground underline decoration-border underline-offset-2 hover:decoration-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45"
                >
                  {t("submit.v2.optimizer.plan.settings")}
                </button>
              </>
            )}
          </p>
        </header>

        {kind && mode ? (
          shown === "plan" ? (
            <WizardScreen active className="flex flex-col gap-5">
              <AgentFieldLock fields={OPTIMIZER_FIELDS}>
                <OptimizerPicker kind={kind} mode={mode} plain />
              </AgentFieldLock>
              {askModels && (
                <AgentFieldLock fields={MODEL_FIELDS}>
                  <ModelsSection kind={kind} choice={choice} />
                </AgentFieldLock>
              )}
              <AgentFieldLock fields={BUDGET_FIELDS}>
                <BudgetSection kind={kind} choice={choice} />
              </AgentFieldLock>
            </WizardScreen>
          ) : (
            <>
              <WizardScreen active={shown === "optimizer"}>
                <AgentFieldLock fields={OPTIMIZER_FIELDS}>
                  <OptimizerPicker kind={kind} mode={mode} />
                </AgentFieldLock>
              </WizardScreen>
              <WizardScreen active={shown === "models"}>
                <AgentFieldLock fields={MODEL_FIELDS}>
                  <ModelsSection kind={kind} choice={choice} />
                </AgentFieldLock>
              </WizardScreen>
              <WizardScreen active={shown === "budget"}>
                <AgentFieldLock fields={BUDGET_FIELDS}>
                  <BudgetSection kind={kind} choice={choice} />
                </AgentFieldLock>
              </WizardScreen>
            </>
          )
        ) : (
          <p className="flex items-start gap-2 text-sm text-muted-foreground" role="status">
            <Warning className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>{t("submit.v2.optimizer.needs_setup")}</span>
          </p>
        )}
      </div>
    </GeneralistAgentShell>
  );
}
