import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const unifiedRunUrl = new URL("../../../shared/types/unified-run.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/types/unified-run") return { shortCircuit: true, url: unifiedRunUrl };
    return nextResolve(specifier, context);
  },
});

const {
  DEFAULT_SHINKA_SETTINGS,
  MAX_EXTRA_OPTIMIZATION_MODELS,
  PARENT_SELECTION_FIELDS,
  SHINKA_LIMITS,
  SHINKA_PATCH_KINDS,
  patchPercent,
  setPatchPercent,
  takesExtraModels,
} = await import("./shinka-settings.ts");

const mixTotal = (s: typeof DEFAULT_SHINKA_SETTINGS) =>
  SHINKA_PATCH_KINDS.reduce((sum, kind) => sum + s[kind], 0);

test("defaults match the backend contract", () => {
  assert.deepEqual(
    { ...DEFAULT_SHINKA_SETTINGS },
    {
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
    },
  );
  assert.ok(Math.abs(mixTotal(DEFAULT_SHINKA_SETTINGS) - 1) < 1e-6);
});

test("every default sits inside its range", () => {
  for (const [field, { min, max }] of Object.entries(SHINKA_LIMITS)) {
    const value = DEFAULT_SHINKA_SETTINGS[field as keyof typeof SHINKA_LIMITS];
    assert.ok(value >= min && value <= max, `${field}=${value} outside ${min}..${max}`);
  }
});

test("each parent-selection strategy shows only its own knobs", () => {
  assert.deepEqual(PARENT_SELECTION_FIELDS.weighted, ["parent_selection_lambda"]);
  assert.deepEqual(PARENT_SELECTION_FIELDS.power_law, ["exploitation_alpha", "exploitation_ratio"]);
  assert.deepEqual(PARENT_SELECTION_FIELDS.beam_search, ["num_beams"]);
});

test("changing one share rescales the other two in proportion", () => {
  const next = setPatchPercent(DEFAULT_SHINKA_SETTINGS, "patch_diff", 40);
  assert.deepEqual([next.patch_diff, next.patch_full, next.patch_cross], [0.4, 0.45, 0.15]);
  assert.ok(Math.abs(mixTotal(next) - 1) < 1e-6);
});

test("the mix still sums to 100% after many awkward edits", () => {
  let s = { ...DEFAULT_SHINKA_SETTINGS };
  const edits: [(typeof SHINKA_PATCH_KINDS)[number], number][] = [
    ["patch_full", 33],
    ["patch_cross", 17],
    ["patch_diff", 71],
    ["patch_full", 3],
    ["patch_cross", 99],
    ["patch_diff", 1],
  ];
  for (const [kind, percent] of edits) {
    s = setPatchPercent(s, kind, percent);
    assert.ok(Math.abs(mixTotal(s) - 1) < 1e-6, `${kind}=${percent} broke the sum`);
    for (const k of SHINKA_PATCH_KINDS) assert.ok(s[k] >= 0 && s[k] <= 1);
  }
});

test("when the other two are zero they split the remainder evenly", () => {
  const allDiff = setPatchPercent(DEFAULT_SHINKA_SETTINGS, "patch_diff", 100);
  assert.deepEqual([allDiff.patch_diff, allDiff.patch_full, allDiff.patch_cross], [1, 0, 0]);
  const next = setPatchPercent(allDiff, "patch_diff", 50);
  assert.deepEqual([next.patch_diff, next.patch_full, next.patch_cross], [0.5, 0.25, 0.25]);
});

test("out-of-range and invalid shares clamp", () => {
  assert.equal(setPatchPercent(DEFAULT_SHINKA_SETTINGS, "patch_cross", 140).patch_cross, 1);
  assert.equal(setPatchPercent(DEFAULT_SHINKA_SETTINGS, "patch_cross", -1).patch_cross, 0);
  assert.equal(setPatchPercent(DEFAULT_SHINKA_SETTINGS, "patch_cross", Number.NaN).patch_cross, 0);
  assert.equal(patchPercent(0.6), 60);
});

test("extra optimization models go only to ShinkaEvolve, up to the backend limit", () => {
  assert.equal(takesExtraModels("shinka_evolve"), true);
  assert.equal(takesExtraModels("auto"), false);
  assert.equal(takesExtraModels("gepa"), false);
  assert.equal(MAX_EXTRA_OPTIMIZATION_MODELS, 4);
});
