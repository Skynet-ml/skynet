"use client";

import { CreditCard, Plus } from "@/shared/ui/icons";
import { Skeleton } from "@/shared/ui/skeleton";
import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { useLocale } from "@/shared/providers";
import { useSettingsModal } from "@/features/settings";
import { useHostedFeaturesAvailable } from "@/shared/hooks/use-hosted-features";
import { useBalance } from "../providers/balance-provider";
import { formatCentsUsd } from "../lib/wallet";

/**
 * Header balance chip: the spendable balance, with the Billing tab's icon.
 * A click opens Billing in settings.
 */
export function BalanceChip({ className }: { className?: string }) {
  const { shown, syncing } = useBalance();
  const { status, totalCents, loading, available, linkNeeded } = shown;
  const { locale } = useLocale();
  const { openTo } = useSettingsModal();
  const hostedAvailable = useHostedFeaturesAvailable();

  // An unlinked Mac app has no wallet, so the header shows no chip.
  if (!hostedAvailable) return null;

  if (loading) {
    return (
      <Skeleton
        width={64}
        borderRadius={8}
        className="h-[26px] any-pointer-coarse:h-11"
        containerClassName={cn("leading-none", className)}
      />
    );
  }

  const empty = available && status === "empty";
  const label = !available
    ? msg(linkNeeded ? "billing.local.link_chip" : "billing.chip.unavailable")
    : empty
      ? msg("billing.chip.empty")
      : formatCentsUsd(totalCents, locale);
  const Icon = empty ? Plus : CreditCard;

  return (
    <button
      type="button"
      onClick={() => openTo("billing")}
      aria-label={formatMsg("billing.chip.aria", { p1: label })}
      aria-busy={syncing || undefined}
      data-setup="balance"
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg border px-2 py-1 text-xs font-semibold transition-colors duration-200 cursor-pointer any-pointer-coarse:min-h-11 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45",
        empty
          ? "border-primary-fill bg-primary-fill text-primary-foreground hover:border-primary hover:bg-primary"
          : "border-border/70 hover:bg-accent",
        !empty && (available && status === "healthy" ? "text-foreground" : "text-muted-foreground"),
        className,
      )}
    >
      <Icon
        className={cn("size-3.5", !empty && status === "healthy" && available && "text-primary")}
        aria-hidden="true"
      />
      <span
        dir={available && !empty ? "ltr" : undefined}
        className={cn("tabular-nums", syncing && "animate-pulse text-muted-foreground")}
      >
        {label}
      </span>
    </button>
  );
}
