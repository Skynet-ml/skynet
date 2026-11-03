"use client";

import * as React from "react";
import type { ModelConfig } from "@/shared/types/api";
import { ModelChip } from "@/shared/ui/model-chip";
import { ModelRoleRow } from "../blackbox/ModelRoleRow";
import { ModelConfigModal } from "../ModelConfigModal";
import { emptyModelConfig } from "../../constants";
import { useModelCatalog, useRecentModelConfigs } from "../../hooks/use-submit-wizard-data";
import { keepEconomy } from "../../lib/model-config";
import { resolveScoringModel, type ScoringModelMode } from "../../lib/model-roles";
import { v2Msg } from "./ArtifactConfirmCard";
import { EconomySwitch } from "./EconomySwitch";

const MOBILE_MODEL_CHIP_CLASS =
  "min-h-[44px] max-lg:[&_button]:min-h-[44px] max-lg:[&_button]:min-w-[44px] max-lg:[&_button]:opacity-100";

/**
 * The metric model picker, shown when the metric calls `llm`. It inherits the
 * reflection model until the user picks another one; picking is one click on
 * the chip, and removing clears it so the contract check reports the gap.
 */
export function MetricModelRow({
  mode,
  explicitModel,
  reflectionModel,
  onChange,
  guided,
}: {
  mode: ScoringModelMode;
  explicitModel: ModelConfig | null;
  reflectionModel: ModelConfig | null;
  onChange: (patch: { metricModelMode: ScoringModelMode; metricModel: ModelConfig | null }) => void;
  guided: boolean;
}) {
  const catalog = useModelCatalog();
  const { recentConfigs, saveToRecent, removeRecentConfig } = useRecentModelConfigs();
  const [editing, setEditing] = React.useState(false);
  const binding = resolveScoringModel({
    usesModel: true,
    mode,
    explicit: explicitModel ?? emptyModelConfig(),
    optimization: reflectionModel ?? emptyModelConfig(),
  });
  const resolved = binding?.resolved ?? null;
  const roleLabel = v2Msg("submit.v2.metric.model.label", guided);

  return (
    <ModelRoleRow
      id="v2-metric-model"
      role={roleLabel}
      binding={v2Msg(
        mode === "inherit" ? "submit.v2.metric.model.inherit" : "submit.v2.metric.model.explicit",
        guided,
      )}
      description={v2Msg("submit.v2.metric.model.desc", guided)}
      actions={
        mode === "explicit" && reflectionModel?.name.trim() ? (
          <button
            type="button"
            onClick={() => onChange({ metricModelMode: "inherit", metricModel: null })}
            className="min-h-[44px] cursor-pointer rounded px-1 text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline lg:min-h-0"
          >
            {v2Msg("submit.v2.metric.model.use_reflection", guided)}
          </button>
        ) : null
      }
    >
      <ModelChip
        roleLabel={roleLabel}
        config={resolved ?? emptyModelConfig()}
        className={MOBILE_MODEL_CHIP_CLASS}
        required
        emptyLabel={
          binding?.pending ? v2Msg("submit.v2.metric.model.not_chosen", guided) : undefined
        }
        catalogModels={catalog?.models}
        onClick={() => setEditing(true)}
        onRemove={
          resolved ? () => onChange({ metricModelMode: "explicit", metricModel: null }) : undefined
        }
      />
      {!resolved && (
        <p role="alert" className="text-xs text-destructive">
          {v2Msg("submit.v2.metric.model.required", guided)}
        </p>
      )}
      {/* An inherited metric model is the reflection model, so its switch decides. */}
      {mode === "explicit" && (
        <EconomySwitch
          id="v2-metric-economy"
          config={explicitModel}
          onChange={(economy) =>
            explicitModel &&
            onChange({
              metricModelMode: "explicit",
              metricModel: { ...explicitModel, economy_mode: economy },
            })
          }
        />
      )}
      <ModelConfigModal
        open={editing}
        onOpenChange={setEditing}
        // Seeded from the model it replaces so a different metric model starts
        // one field away, not from blank.
        config={resolved ?? emptyModelConfig()}
        onSave={(config) => {
          onChange({ metricModelMode: "explicit", metricModel: keepEconomy(config, resolved) });
          saveToRecent(keepEconomy(config, null));
          setEditing(false);
        }}
        roleLabel={roleLabel}
        catalogModels={catalog?.models}
        recentConfigs={recentConfigs}
        onRemoveRecent={removeRecentConfig}
      />
    </ModelRoleRow>
  );
}
