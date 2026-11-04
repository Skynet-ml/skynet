"use client";

import { useRef, type KeyboardEvent } from "react";

import { Warning } from "@/shared/ui/icons";
import { Label } from "@/shared/ui/primitives/label";
import { msg } from "@/shared/lib/messages";
import type { OptimizerTarget, DataMode, OptimizerChoice } from "@/shared/types/unified-run";
import { getActiveDir } from "@/shared/lib/runtime-locale";

import { useWizardV2 } from "../../hooks/use-unified-wizard";
import {
  optimizerChoiceIssue,
  optimizerOptions,
  type OptimizerOptionView,
} from "../../lib/optimizer-options";
import { optimizerSources } from "../../lib/optimizer-sources";
import { EngineCard } from "../blackbox/EngineCard";
import { StepCard } from "../blackbox/shared";
import { v2Msg } from "./ArtifactConfirmCard";
import { OptimizerSourceLinks } from "./OptimizerSourceLinks";

// Product names stay untranslated in every locale; Auto is the one generic word.
const OPTIMIZER_NAMES: Record<Exclude<OptimizerChoice, "auto">, string> = {
  dspy_gepa: "DSPy GEPA",
  dspy_miprov2: "MIPROv2",
  dspy_bootstrap_fewshot: "BootstrapFewShot",
  gepa: "GEPA",
  best_of_n: "Best-of-N",
  autoresearch: "AutoResearch",
  meta_harness: "Meta-Harness",
  autosaddler: "AutoSaddler",
  shinka_evolve: "ShinkaEvolve",
};

/** The display name of an optimizer choice in the active locale. */
export function optimizerLabel(choice: OptimizerChoice, guided: boolean): string {
  return choice === "auto" ? v2Msg("submit.v2.optimizer.option.auto.label", guided) : OPTIMIZER_NAMES[choice];
}

// Only a DSPy program offers Auto; a repository run lists the engines alone.
function optimizerDescription(view: OptimizerOptionView, guided: boolean): string {
  if (view.id !== "auto") return v2Msg(`submit.v2.optimizer.option.${view.id}.desc`, guided);
  return v2Msg("submit.v2.optimizer.option.auto.desc_dspy", guided);
}

/**
 * Step 3's optimizer choice: only the optimizers that fit the task and inputs,
 * Auto first and preselected. Picking never happens on the user's behalf: when
 * an upstream edit rules the current choice out, the card says so and keeps it.
 * `plain` (Standard) words each card the plain way.
 */
export function OptimizerPicker({
  kind,
  mode,
  plain = false,
}: {
  kind: OptimizerTarget;
  mode: DataMode;
  plain?: boolean;
}) {
  const { state, patchOptimizer, optimizerCatalog, optimizerCatalogLoading, guided } = useWizardV2();
  const t = (key: string, params?: Record<string, string>) => v2Msg(key, guided, params);
  const choice = state.optimizer.choice;
  const options = optimizerOptions({ kind, mode, catalog: optimizerCatalog, guided, choice });
  const issue = optimizerChoiceIssue({ kind, mode, catalog: optimizerCatalog, choice });
  const selectedIndex = options.findIndex((option) => option.id === choice);
  const catalog =
    optimizerCatalog?.artifact_kind === kind && optimizerCatalog.data_mode === mode
      ? optimizerCatalog
      : null;
  // Roving tabindex: the checked card takes the Tab stop, else the first.
  const focusIndex = selectedIndex >= 0 ? selectedIndex : 0;
  const groupRef = useRef<HTMLDivElement>(null);

  const choose = (index: number) => {
    const option = options[index];
    if (option?.available) patchOptimizer({ choice: option.id });
  };

  // Arrow keys move between cards like native radios, skipping blocked ones;
  // inline arrows follow the reading direction.
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const rtl = getActiveDir() === "rtl";
    const steps: Record<string, number> = {
      ArrowDown: 1,
      ArrowUp: -1,
      ArrowRight: rtl ? -1 : 1,
      ArrowLeft: rtl ? 1 : -1,
    };
    let target: number | null = null;
    if (event.key === "Home") target = options.findIndex((o) => o.available);
    else if (event.key === "End") target = options.findLastIndex((o) => o.available);
    else {
      const step = steps[event.key];
      if (step === undefined) return;
      const from = Math.max(0, selectedIndex);
      for (let n = 1; n <= options.length; n++) {
        const i = (from + step * n + options.length * n) % options.length;
        if (options[i]?.available) {
          target = i;
          break;
        }
      }
    }
    if (target === null || target < 0) return;
    event.preventDefault();
    choose(target);
    groupRef.current?.querySelectorAll<HTMLElement>('[role="radio"]')[target]?.focus();
  };

  return (
    <StepCard title={t("submit.v2.optimizer.picker.title")} description={t("submit.v2.optimizer.picker.desc")}>
      <div id="v2-optimizers" tabIndex={-1} className="space-y-2 outline-none">
        <Label>{t("submit.v2.optimizer.picker.label")}</Label>
        <div
          ref={groupRef}
          role="radiogroup"
          aria-label={t("submit.v2.optimizer.picker.carousel_aria")}
          onKeyDown={onKeyDown}
          className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,13rem),1fr))] gap-3"
        >
          {options.map((option, i) => {
            const checked = choice === option.id;
            const label = optimizerLabel(option.id, guided);
            const sources = optimizerSources(option.id);
            // Links cannot nest inside the radio button, so they sit beside it
            // and overlay the slot EngineCard reserves at the card's foot,
            // in the bottom-end corner.
            return (
              <div key={option.id} className="group/card relative flex">
                <button
                  type="button"
                  role="radio"
                  aria-checked={checked}
                  aria-disabled={!option.available || undefined}
                  tabIndex={i === focusIndex ? 0 : -1}
                  onClick={() => choose(i)}
                  className={`flex flex-1 flex-col overflow-hidden rounded-xl border bg-background/60 text-start transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
                    checked
                      ? "border-primary ring-1 ring-primary"
                      : "border-border/50 group-hover/card:border-border"
                  } ${option.available ? "cursor-pointer" : "cursor-not-allowed opacity-70"}`}
                >
                  <EngineCard
                    engine={{
                      id: option.id,
                      label,
                      description: optimizerDescription(option, guided || plain),
                      available: option.available,
                      unavailable_reason: option.unavailableReason,
                    }}
                    blocked={!option.available}
                    blockedNote={option.unavailableReason ?? t("submit.v2.optimizer.unavailable")}
                    footerSlot={sources.length > 0}
                  />
                </button>
                <OptimizerSourceLinks
                  optimizerName={label}
                  sources={sources}
                  className="absolute end-3 bottom-3"
                />
              </div>
            );
          })}
        </div>
      </div>

      {optimizerCatalogLoading && !catalog && (
        <p className="text-xs text-muted-foreground" role="status">
          {msg("submit.blackbox.engines.checking")}
        </p>
      )}

      {catalog && !catalog.sandbox_available && (
        <p className="flex items-start gap-2 text-xs text-[var(--warning)]" role="status">
          <Warning className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span dir="auto">{catalog.sandbox_reason?.trim() || t("submit.v2.optimizer.sandbox_unavailable")}</span>
        </p>
      )}

      {issue && (
        <p className="flex items-start gap-2 text-xs text-[var(--warning)]" role="status">
          <Warning className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span dir="auto">
            {issue.kind === "not_allowed"
              ? t("submit.v2.optimizer.issue.not_allowed", { p1: optimizerLabel(choice, guided) })
              : (issue.reason ?? t("submit.v2.optimizer.issue.unavailable"))}
          </span>
        </p>
      )}
    </StepCard>
  );
}
