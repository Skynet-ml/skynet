import type {
  BlackboxShinkaParentSelection,
  BlackboxShinkaSettings,
  ModelConfig,
} from "@/shared/types/api";
import {
  resolveOptimizer,
  type OptimizerTarget,
  type OptimizerChoice,
} from "@/shared/types/unified-run";

/**
 * ShinkaEvolve's defaults, mirroring `BlackboxShinkaSettings` on the backend.
 * The three attempt counts are not pinned by the contract; they follow the
 * upstream `EvolutionConfig` defaults.
 */
export const DEFAULT_SHINKA_SETTINGS: Readonly<BlackboxShinkaSettings> = {
  editor: "single_call",
  num_islands: 2,
  migration_interval: 10,
  migration_rate: 0,
  parent_selection: "weighted",
  parent_selection_lambda: 10,
  exploitation_alpha: 1,
  exploitation_ratio: 0.2,
  num_beams: 5,
  patch_diff: 0.6,
  patch_full: 0.3,
  patch_cross: 0.1,
  max_patch_attempts: 5,
  max_patch_resamples: 3,
  archive_size: 40,
  num_archive_inspirations: 1,
  num_top_k_inspirations: 1,
  elite_selection_ratio: 0.3,
  use_text_feedback: true,
  novelty: false,
  code_embed_sim_threshold: 0.99,
  max_novelty_attempts: 3,
  meta_notes: true,
  meta_rec_interval: 10,
  meta_max_recommendations: 5,
  max_parallel_evaluations: 2,
  max_parallel_proposals: 2,
};

export type ShinkaNumberField = {
  [K in keyof BlackboxShinkaSettings]: BlackboxShinkaSettings[K] extends number ? K : never;
}[keyof BlackboxShinkaSettings];

export type ShinkaPatchKind = "patch_diff" | "patch_full" | "patch_cross";

/** The numeric settings with a stepper of their own; the patch mix is edited as a whole. */
export type ShinkaStepperField = Exclude<ShinkaNumberField, ShinkaPatchKind>;

/** The change kinds, in the order the panel lists them. */
export const SHINKA_PATCH_KINDS: readonly ShinkaPatchKind[] = [
  "patch_diff",
  "patch_full",
  "patch_cross",
];

/** The inclusive ranges the backend validates, with the step the panel's steppers move by. */
export const SHINKA_LIMITS: Readonly<
  Record<ShinkaStepperField, { min: number; max: number; step: number }>
> = {
  num_islands: { min: 1, max: 8, step: 1 },
  migration_interval: { min: 1, max: 100, step: 1 },
  migration_rate: { min: 0, max: 1, step: 0.05 },
  parent_selection_lambda: { min: 0.1, max: 100, step: 0.5 },
  exploitation_alpha: { min: 0, max: 10, step: 0.1 },
  exploitation_ratio: { min: 0, max: 1, step: 0.05 },
  num_beams: { min: 1, max: 20, step: 1 },
  max_patch_attempts: { min: 1, max: 10, step: 1 },
  max_patch_resamples: { min: 1, max: 10, step: 1 },
  archive_size: { min: 1, max: 500, step: 5 },
  num_archive_inspirations: { min: 0, max: 10, step: 1 },
  num_top_k_inspirations: { min: 0, max: 10, step: 1 },
  elite_selection_ratio: { min: 0, max: 1, step: 0.05 },
  code_embed_sim_threshold: { min: 0.5, max: 1, step: 0.01 },
  max_novelty_attempts: { min: 1, max: 10, step: 1 },
  meta_rec_interval: { min: 1, max: 100, step: 1 },
  meta_max_recommendations: { min: 1, max: 20, step: 1 },
  max_parallel_evaluations: { min: 1, max: 8, step: 1 },
  max_parallel_proposals: { min: 1, max: 8, step: 1 },
};

/** The knobs each parent-selection strategy reads; the panel shows only those. */
export const PARENT_SELECTION_FIELDS: Readonly<
  Record<BlackboxShinkaParentSelection, readonly ShinkaStepperField[]>
> = {
  weighted: ["parent_selection_lambda"],
  power_law: ["exploitation_alpha", "exploitation_ratio"],
  beam_search: ["num_beams"],
};

/** How many optimization models ShinkaEvolve takes beyond the first. */
export const MAX_EXTRA_OPTIMIZATION_MODELS = 4;

/**
 * Set one change kind's share of the mix, in whole percent, and rescale the
 * other two in proportion so the mix always sums to 100%. Working in whole
 * percents keeps the three shares summing to exactly 1, which the backend
 * checks within 1e-6. When the other two are both zero they split the rest
 * evenly.
 */
export function setPatchPercent(
  settings: BlackboxShinkaSettings,
  kind: ShinkaPatchKind,
  percent: number,
): BlackboxShinkaSettings {
  const value = Math.round(Math.min(100, Math.max(0, Number.isFinite(percent) ? percent : 0)));
  const [first, second] = SHINKA_PATCH_KINDS.filter((k) => k !== kind) as [
    ShinkaPatchKind,
    ShinkaPatchKind,
  ];
  const rest = 100 - value;
  const otherTotal = settings[first] + settings[second];
  const firstPercent = Math.round(
    otherTotal > 0 ? (rest * settings[first]) / otherTotal : rest / 2,
  );
  return {
    ...settings,
    [kind]: value / 100,
    [first]: firstPercent / 100,
    [second]: (rest - firstPercent) / 100,
  };
}

/** The share as the whole percent the panel shows. */
export function patchPercent(share: number): number {
  return Math.round(share * 100);
}

/**
 * The settings block to submit. A ShinkaEvolve run always sends it, with the
 * defaults when the panel was never touched; black-box Auto, where
 * ShinkaEvolve is one lane, sends it only once the user edited it (`settings`
 * non-null), so an untouched Auto run keeps the server's defaults.
 */
export function submittedShinka(
  settings: BlackboxShinkaSettings | null,
  choice: OptimizerChoice,
  kind: OptimizerTarget,
): BlackboxShinkaSettings | undefined {
  const resolved = resolveOptimizer(choice, kind);
  if (resolved === "shinka_evolve") return { ...(settings ?? DEFAULT_SHINKA_SETTINGS) };
  if (resolved === "auto") return settings ? { ...settings } : undefined;
  return undefined;
}

/** Whether the choice takes extra optimization models: only ShinkaEvolve samples from a pool. */
export function takesExtraModels(choice: OptimizerChoice): boolean {
  return choice === "shinka_evolve";
}

/** The extra optimization models to submit: chosen ones only, and only for ShinkaEvolve. */
export function submittedExtraModels(
  models: readonly ModelConfig[],
  choice: OptimizerChoice,
): ModelConfig[] | undefined {
  if (!takesExtraModels(choice)) return undefined;
  const chosen = models.filter((model) => model.name.trim());
  return chosen.length > 0 ? chosen.slice(0, MAX_EXTRA_OPTIMIZATION_MODELS) : undefined;
}

/** A stored run's settings over the defaults, so a clone of an older run still fills every field. */
export function shinkaSettingsFrom(
  stored: Partial<BlackboxShinkaSettings> | null | undefined,
): BlackboxShinkaSettings {
  return { ...DEFAULT_SHINKA_SETTINGS, ...(stored ?? {}) };
}
