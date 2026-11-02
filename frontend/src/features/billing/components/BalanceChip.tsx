"use client";

import { Skeleton } from "@/shared/ui/skeleton";
import { Button } from "@/shared/ui/primitives/button";
import * as React from "react";
import { Coins, Plus } from "@/shared/ui/icons";
import { msg, formatMsg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { useLocale } from "@/shared/providers";
import { useSettingsModal } from "@/features/settings";
import { useHostedFeaturesAvailable } from "@/shared/hooks/use-hosted-features";
import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/primitives/popover";
import { useBalance } from "../providers/balance-provider";
import { formatCentsUsd } from "../lib/wallet";
import { LinkAccountPrompt } from "./LinkAccountPrompt";

/**
 * Header balance chip — the spine of the billing UI.
 *
 * Sits inline-end beside the language switcher. Shows the total spendable balance
 * (free grant + purchased). Theming is calm by design: the brand `primary` marks a healthy balance and the
 * primary "Add funds" affordance only; a low balance reads as quiet taupe, not
 * an alarm.
 */
export function BalanceChip({ className }: { className?: string }) {
  const { wallet, shown, syncing } = useBalance();
  const { status, totalCents, loading, available, linkNeeded, hosted } = shown;
  const { locale } = useLocale();
  const { openTo } = useSettingsModal();
  const [open, setOpen] = React.useState(false);
  const hostedAvailable = useHostedFeaturesAvailable();

  // An unlinked Mac app has no wallet, so the header shows no chip at all.
  if (!hostedAvailable) return null;

  if (loading) {
    return (
      // The chip is a button, so on a coarse pointer it takes the 44px floor.
      <Skeleton
        width={64}
        borderRadius={8}
        className="h-[26px] any-pointer-coarse:h-11"
        containerClassName={cn("leading-none", className)}
      />
    );
  }

  // One trigger, three visual registers. Healthy spends the gold accent; low and
  // empty stay in the warm neutrals so the chip never shouts. Empty reframes the
  // chip itself as the "add funds" call to action.
  const triggerTone = !available
    ? "border-border/70 text-muted-foreground hover:bg-accent"
    : status === "healthy"
      ? "border-border/70 text-foreground hover:bg-accent"
      : status === "low"
        ? "border-border/70 text-muted-foreground hover:bg-accent"
        : "border-primary-fill bg-primary-fill text-primary-foreground hover:border-primary hover:bg-primary";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={formatMsg("billing.chip.aria", {
            p1: !available
              ? msg(linkNeeded ? "billing.local.link_chip" : "billing.chip.unavailable")
              : formatCentsUsd(totalCents, locale),
          })}
          aria-busy={syncing || undefined}
          data-setup="balance"
          className={cn(
            "inline-flex items-center gap-1.5 rounded-lg border px-2 py-1 text-xs font-semibold transition-colors duration-200 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45",
            triggerTone,
            className,
          )}
        >
          {!available ? (
            <>
              <Coins className="size-3.5 text-muted-foreground" aria-hidden="true" />
              <span>
                {msg(linkNeeded ? "billing.local.link_chip" : "billing.chip.unavailable")}
              </span>
            </>
          ) : status === "empty" ? (
            <>
              <Plus className="size-3.5" aria-hidden="true" />
              <span>{msg("billing.chip.empty")}</span>
            </>
          ) : (
            <>
              <Coins
                className={cn(
                  "size-3.5",
                  status === "healthy" ? "text-primary" : "text-muted-foreground",
                )}
                aria-hidden="true"
              />
              {/* Post-checkout sync [FG-3]: a quiet shimmer over the *prior* balance
                  until the webhook lands, so the chip never flashes a false zero. */}
              <span
                dir="ltr"
                className={cn("tabular-nums", syncing && "animate-pulse text-muted-foreground")}
              >
                {formatCentsUsd(totalCents, locale)}
              </span>
            </>
          )}
        </button>
      </PopoverTrigger>

      <PopoverContent align="end" className="w-72 p-0">
        <div className="flex flex-col">
          <div className="flex items-baseline justify-between gap-3 px-4 pt-4 pb-3">
            <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {msg("billing.popover.title")}
            </span>
          </div>

          {linkNeeded ? (
            <LinkAccountPrompt className="px-4 pb-4" />
          ) : !available ? (
            <p className="px-4 pb-4 text-xs text-muted-foreground">
              {msg("billing.popover.unavailable")}
            </p>
          ) : hosted ? (
            // Hosted sends one total, so the Mac app shows no paid/grant split.
            <div className="px-4 pb-3 text-start">
              <span
                dir="ltr"
                className="inline-block text-2xl font-semibold text-foreground tabular-nums"
              >
                {formatCentsUsd(totalCents, locale)}
              </span>
              <p className="pt-1 text-xs text-muted-foreground">
                {msg("billing.local.hosted_note")}
              </p>
              {status === "low" && (
                <p className="pt-1 text-xs text-muted-foreground/80">
                  {msg("billing.chip.low_note")}
                </p>
              )}
            </div>
          ) : (
            <>
              {/* Balance sits directly under the title. An inline-block (not a
                  full-width `text-right` block) lets the parent's logical
                  `text-start` place it at the inline-start in both LTR and RTL,
                  while `dir="ltr"` keeps the numerals themselves left-to-right. */}
              <div className="px-4 pb-3 text-start">
                <span
                  dir="ltr"
                  className="inline-block text-2xl font-semibold text-foreground tabular-nums"
                >
                  {formatCentsUsd(totalCents, locale)}
                </span>
              </div>
              <dl className="flex flex-col gap-2 border-t border-border/40 px-4 py-3 text-xs">
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-muted-foreground">{msg("billing.popover.paid")}</dt>
                  <dd dir="ltr" className="font-medium text-foreground tabular-nums">
                    {formatCentsUsd(wallet.paidBalanceCents, locale)}
                  </dd>
                </div>
                {/* Only legacy accounts still hold a grant — new accounts have
                    none, so an empty 0/0 row would advertise a perk that no
                    longer exists. */}
                {wallet.freeGrant.centsTotal > 0 && (
                  <div className="flex items-center justify-between gap-3">
                    <dt className="text-muted-foreground">{msg("billing.popover.free_grant")}</dt>
                    <dd dir="ltr" className="font-medium text-foreground tabular-nums">
                      {formatCentsUsd(wallet.freeGrant.centsRemaining, locale)}
                      <span className="text-muted-foreground">
                        {" / "}
                        {formatCentsUsd(wallet.freeGrant.centsTotal, locale)}
                      </span>
                    </dd>
                  </div>
                )}
                {/* Low balance reads as an operational metric, not an alarm: one
                    calm factual line in the warm neutrals, no red, no urgency. */}
                {status === "low" && (
                  <p className="text-muted-foreground/80">{msg("billing.chip.low_note")}</p>
                )}
              </dl>
            </>
          )}

          <div className="flex items-center gap-2 border-t border-border/40 p-3">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setOpen(false);
                openTo("billing");
              }}
              className="w-full"
            >
              {msg("billing.action.view_wallet")}
            </Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
