import type { OptimizerChoice } from "@/shared/types/unified-run";

// Product names stay untranslated in every locale; Auto is the one generic word.
export const OPTIMIZER_NAMES: Readonly<Record<Exclude<OptimizerChoice, "auto">, string>> = {
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
