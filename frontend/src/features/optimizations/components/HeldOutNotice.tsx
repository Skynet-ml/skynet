"use client";

import { msg } from "@/shared/lib/messages";
import { Warning } from "@/shared/ui/icons";
import type { UnifiedSplitCounts } from "@/shared/types/unified-run";

/** Report whether the split explicitly says no held-out final score exists. */
export function isNotHeldOut(splitCounts: unknown): boolean {
  const split = splitCounts as Partial<UnifiedSplitCounts> | null | undefined;
  return !!split && typeof split === "object" && "held_out" in split && split.held_out === false;
}

/** Say so when too few cases left none for a held-out final score. */
export function HeldOutNotice({ splitCounts }: { splitCounts: unknown }) {
  if (!isNotHeldOut(splitCounts)) {
    return null;
  }
  return (
    <p
      role="note"
      data-notice="not-held-out"
      className="flex items-start gap-2 rounded-xl border border-[var(--warning-border)] bg-[var(--warning-dim)] px-4 py-3 text-xs text-[var(--warning)]"
    >
      <Warning className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      <span>{msg("optimization.reward.not_held_out")}</span>
    </p>
  );
}
