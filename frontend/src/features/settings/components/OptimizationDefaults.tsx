"use client";

import * as React from "react";

import { ModelConfigModal, optimizerLabel, useRecentModelConfigs } from "@/features/submit";
import { cn } from "@/shared/lib/utils";
import { msg } from "@/shared/lib/messages";
import { cachedCatalog, getModelCatalog } from "@/shared/lib/model-catalog";
import { modelDisplayName } from "@/shared/lib/model-provider";
import type { CatalogModel, ModelConfig } from "@/shared/types/api";
import {
  BLACKBOX_OPTIMIZERS,
  type OptimizerChoice,
} from "@/shared/types/unified-run";
import { ArrowCounterClockwise, Cpu, Sparkle, Target } from "@/shared/ui/icons";
import { ModelChip } from "@/shared/ui/model-chip";
import { NumberInput } from "@/shared/ui/number-input";
import { Button } from "@/shared/ui/primitives/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { SettingsRow } from "@/shared/ui/settings-row";
import { TOUCH_FIELD_SM } from "@/shared/ui/touch";

import { useUserPrefs } from "../hooks/use-user-prefs";

const ENGINES: readonly OptimizerChoice[] = ["auto", ...BLACKBOX_OPTIMIZERS];
// Mirrors the wizard's own default, the backend BlackboxBudget default.
const DEFAULT_MAX_RUNS = 200;

type ModelRole = "optimizerTaskModel" | "optimizerReflectionModel";

/**
 * The defaults a new optimization starts from: engine, task and reflection
 * models, and how many scorer runs it may make. The wizard reads them when it
 * opens; Guided runs on them without showing the Optimizer step.
 */
export function OptimizationDefaults() {
  const { prefs, setPref } = useUserPrefs();
  const [editing, setEditing] = React.useState<ModelRole | null>(null);
  const { recentConfigs, saveToRecent, removeRecentConfig } = useRecentModelConfigs();
  const [catalogModels, setCatalogModels] = React.useState<CatalogModel[] | null>(null);
  React.useEffect(() => {
    if (catalogModels) return;
    // Read the localStorage-backed cache after mount, not in useState, so the
    // first client render matches the server's.
    const cached = cachedCatalog();
    if (cached) {
      setCatalogModels(cached.models);
      return;
    }
    let cancelled = false;
    getModelCatalog()
      .then((c) => {
        if (!cancelled) setCatalogModels(c.models);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [catalogModels]);

  // An unpinned model is stored empty and resolved to the catalog default
  // each time a wizard opens, so a new default reaches everyone who never
  // pinned one.
  const bestNow = catalogModels?.find((model) => model.is_default);
  const automaticLabel = bestNow
    ? msg("settings.optimization.model.automatic_now", {
        model: modelDisplayName(bestNow.value, catalogModels ?? undefined),
      })
    : msg("settings.optimization.model.automatic");
  const strongestNow = catalogModels?.find((model) => model.is_strongest);
  const reflectionAutomaticLabel = strongestNow
    ? msg("settings.optimization.model.automatic_strongest", {
        model: modelDisplayName(strongestNow.value, catalogModels ?? undefined),
      })
    : automaticLabel;

  const modelRow = (role: ModelRole, label: string, description: string) => (
    <SettingsRow icon={Cpu} label={label} description={description}>
      <div className="flex min-w-0 items-center gap-1">
        <ModelChip
          config={prefs[role]}
          emptyLabel={
            role === "optimizerReflectionModel" ? reflectionAutomaticLabel : automaticLabel
          }
          catalogModels={catalogModels ?? undefined}
          onClick={() => setEditing(role)}
        />
        {prefs[role].name ? (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={msg("settings.optimization.model.reset")}
            title={msg("settings.optimization.model.reset")}
            onClick={() => setPref(role, { name: "" })}
          >
            <ArrowCounterClockwise aria-hidden="true" />
          </Button>
        ) : null}
      </div>
    </SettingsRow>
  );
  const editingLabel =
    editing === "optimizerReflectionModel"
      ? msg("settings.optimization.reflection_model.label")
      : msg("settings.optimization.task_model.label");

  return (
    <section id="settings-optimization" className="space-y-1 pt-4">
      <h3 className="px-1 pb-1 text-sm font-semibold text-foreground">
        {msg("settings.optimization.title")}
      </h3>
      <SettingsRow
        icon={Sparkle}
        label={msg("settings.optimization.engine.label")}
        description={msg("settings.optimization.engine.description")}
      >
        <Select
          value={prefs.optimizerEngine}
          onValueChange={(v) => setPref("optimizerEngine", v as OptimizerChoice)}
        >
          <SelectTrigger
            size="sm"
            className={cn(TOUCH_FIELD_SM, "w-full min-w-0 sm:w-auto sm:min-w-[160px]")}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ENGINES.map((engine) => (
              <SelectItem key={engine} value={engine}>
                {optimizerLabel(engine, false)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </SettingsRow>

      {modelRow(
        "optimizerTaskModel",
        msg("settings.optimization.task_model.label"),
        msg("settings.optimization.task_model.description"),
      )}
      {modelRow(
        "optimizerReflectionModel",
        msg("settings.optimization.reflection_model.label"),
        msg("settings.optimization.reflection_model.description"),
      )}

      <SettingsRow
        icon={Target}
        label={msg("settings.optimization.max_runs.label")}
        description={msg("settings.optimization.max_runs.description")}
      >
        <NumberInput
          id="settings-optimizer-max-runs"
          size="sm"
          value={prefs.optimizerMaxRuns ?? DEFAULT_MAX_RUNS}
          onChange={(value) => setPref("optimizerMaxRuns", value)}
          min={1}
          max={100000}
          step={10}
        />
      </SettingsRow>

      <ModelConfigModal
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
        config={editing ? prefs[editing] : ({ name: "" } satisfies ModelConfig)}
        onSave={(cfg) => {
          if (!editing) return;
          saveToRecent(cfg);
          setPref(editing, cfg);
        }}
        roleLabel={editingLabel}
        catalogModels={catalogModels ?? undefined}
        recentConfigs={recentConfigs}
        onRemoveRecent={removeRecentConfig}
      />
    </section>
  );
}
