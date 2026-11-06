import type { ComponentType } from "react";

import {
  ArrowsClockwise,
  Crosshair,
  Cube,
  DiceFive,
  GitBranch,
  MagicWand,
  PushPin,
  Robot,
  SquaresFour,
  TreeStructure,
  Wrench,
} from "@/shared/ui/icons";
import { cn } from "@/shared/lib/utils";

const OPTIMIZER_ICONS: Record<string, ComponentType<{ className?: string }>> = {
  auto: MagicWand,
  dspy_gepa: TreeStructure,
  dspy_miprov2: Crosshair,
  dspy_bootstrap_fewshot: PushPin,
  gepa: GitBranch,
  best_of_n: DiceFive,
  autoresearch: Robot,
  meta_harness: Wrench,
  autosaddler: ArrowsClockwise,
  shinka_evolve: SquaresFour,
};

/**
 * An optimizer's mark, the way ProviderLogo marks a model: its icon from the
 * optimizer picker on a small rounded tile. Unknown ids get a plain cube.
 */
export function OptimizerLogo({
  id,
  size = 16,
  className,
}: {
  id: string;
  size?: number;
  className?: string;
}) {
  const Icon = OPTIMIZER_ICONS[id] ?? Cube;
  return (
    <span
      aria-hidden="true"
      className={cn("grid shrink-0 place-items-center bg-accent text-muted-foreground", className)}
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.28) }}
    >
      <Icon className="size-[55%]" />
    </span>
  );
}
