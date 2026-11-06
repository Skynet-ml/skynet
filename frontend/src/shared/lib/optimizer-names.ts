import type { OptimizerChoice } from "@/shared/types/unified-run";

// Product names stay untranslated in every locale; Auto is the one generic word.
export const OPTIMIZER_NAMES: Readonly<Record<Exclude<OptimizerChoice, "auto">, string>> = {
  gepa: "GEPA",
  best_of_n: "Best-of-N",
  autoresearch: "AutoResearch",
  meta_harness: "Meta-Harness",
  autosaddler: "AutoSaddler",
  shinka_evolve: "ShinkaEvolve",
};
