import type { OptimizerChoice } from "@/shared/types/unified-run";

/** One primary source behind an optimizer: its paper, or its original repo when it has no paper. */
export interface OptimizerSource {
  kind: "paper" | "code";
  title: string;
  url: string;
}

// Every URL here was checked against the primary page (arXiv abstract or the
// original repository). Best-of-N, Auto and anything else without a primary
// source get no entry rather than a guessed one.
const GEPA_PAPER: OptimizerSource = {
  kind: "paper",
  title: "GEPA: Reflective Prompt Evolution Can Outperform Reinforcement Learning",
  url: "https://arxiv.org/abs/2507.19457",
};

export const OPTIMIZER_SOURCES: Partial<Record<OptimizerChoice, readonly OptimizerSource[]>> = {
  gepa: [GEPA_PAPER],
  autoresearch: [
    {
      kind: "code",
      title: "karpathy/autoresearch",
      url: "https://github.com/karpathy/autoresearch",
    },
  ],
  meta_harness: [
    {
      kind: "paper",
      title: "Meta-Harness: End-to-End Optimization of Model Harnesses",
      url: "https://arxiv.org/abs/2603.28052",
    },
  ],
  autosaddler: [
    {
      kind: "paper",
      title: "AutoSaddler: Automatic Harness Optimization with Durable Updates from Agent Execution Traces",
      url: "https://arxiv.org/abs/2608.23041",
    },
  ],
  shinka_evolve: [
    {
      kind: "paper",
      title: "ShinkaEvolve: Towards Open-Ended And Sample-Efficient Program Evolution",
      url: "https://arxiv.org/abs/2509.19349",
    },
  ],
};

/** The primary sources for an optimizer choice; empty when it has none. */
export function optimizerSources(choice: OptimizerChoice): readonly OptimizerSource[] {
  return OPTIMIZER_SOURCES[choice] ?? [];
}
