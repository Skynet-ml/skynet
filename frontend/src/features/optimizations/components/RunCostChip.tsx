"use client";

import { Coins } from "@/shared/ui/icons";
import { useLocale } from "@/shared/providers";
import { useSettingsModal } from "@/features/settings";
import { useHostedFeaturesAvailable } from "@/shared/hooks/use-hosted-features";
import { msg } from "@/shared/lib/messages";
import { formatCentsUsd } from "@/features/billing";
import type { JobExecutionBudget } from "@/shared/types/execution-budget";
import { runCostCents } from "../lib/run-billing";

/**
 * Compact per-run cost affordance for the detail header.
 *
 * Shows what the run cost, from the worker's billing stamp or, for runs
 * charged through the execution budget, from that budget.
 * It's a quiet button — clicking opens the run's Usage and cost tab, or the
 * wallet where that tab isn't available. Renders nothing until the run has a
 * charge, and stays hidden on pairs, which carry no billing of their own.
 */
export function RunCostChip({
  details,
  budget,
  running = false,
  onOpen,
}: {
  details?: Record<string, unknown>;
  budget?: JobExecutionBudget | null;
  running?: boolean;
  onOpen?: () => void;
}) {
  const { locale } = useLocale();
  const { openTo } = useSettingsModal();
  const hostedAvailable = useHostedFeaturesAvailable();
  const cents = runCostCents(details, budget, running);
  if (cents == null) return null;

  // An unlinked Mac app has no wallet tab to open, so the cost is plain text there.
  if (!onOpen && !hostedAvailable) {
    return (
      <span dir="ltr" className="flex items-center gap-1.5 tabular-nums">
        <Coins className="size-3.5" aria-hidden="true" />
        {formatCentsUsd(cents, locale)}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={onOpen ?? (() => openTo("billing"))}
      title={onOpen ? msg("usage_tab.open") : msg("billing.action.view_wallet")}
      aria-label={onOpen ? msg("usage_tab.open") : msg("billing.action.view_wallet")}
      dir="ltr"
      className="flex min-h-[44px] items-center gap-1.5 tabular-nums transition-colors hover:text-foreground sm:min-h-0 [@media(hover:none)_and_(pointer:coarse)]:min-h-[44px]"
    >
      <Coins className="size-3.5" aria-hidden="true" />
      {formatCentsUsd(cents, locale)}
    </button>
  );
}
