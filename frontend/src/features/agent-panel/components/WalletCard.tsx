"use client";

import * as React from "react";
import { formatCentsUsd } from "@/features/billing";
import { formatMsg, msg } from "@/shared/lib/messages";

import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";

import type { AgentToolCall } from "@/shared/ui/agent/types";

import { ToolCallRow } from "./ToolCallRow";
import { StatTile } from "./result-card-atoms";
import { walletView, type WalletView } from "../lib/wallet-view";

interface WalletCardProps {
  call: AgentToolCall;
}

/** A cent amount as its dollar value. */
function fmtUsd(cents: number): string {
  return formatCentsUsd(cents, getActiveIntlLocale());
}

function buildSummary(view: WalletView | null, isRunning: boolean): string | null {
  if (isRunning || !view) return null;
  if (view.kind === "not_linked") return msg("billing.local.link_prompt");
  if (view.kind === "unavailable") return msg("billing.popover.unavailable");
  return formatMsg("auto.features.agent.panel.components.walletcard.summary", {
    p1: fmtUsd(view.totalCents),
  });
}

/**
 * Result card for ``get_wallet_for_agent`` — the caller's spendable balance in
 * dollars (free grant + paid balance) as a headline, a free-grant / paid-balance
 * split, and the most recent ledger entries. In the Mac app it shows the linked
 * account's hosted balance, or a sign-in prompt when no account is linked.
 */
export function WalletCard({ call }: WalletCardProps) {
  const payload = (call.payload ?? {}) as Record<string, unknown>;
  const view = walletView(payload.result);
  const summary = buildSummary(view, call.status === "running");

  if (!view) {
    return <ToolCallRow call={call} summary={summary} />;
  }

  if (view.kind === "not_linked" || view.kind === "unavailable") {
    const note =
      view.kind === "not_linked" ? "billing.local.link_prompt" : "billing.popover.unavailable";
    const customBody = <p className="text-[0.75rem] text-muted-foreground">{msg(note)}</p>;
    return <ToolCallRow call={call} summary={summary} customBody={customBody} />;
  }

  const headline = (
    <div dir="ltr" className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
      <span className="text-[1.25rem] font-semibold tabular-nums text-foreground">
        {fmtUsd(view.totalCents)}
      </span>
      <span className="text-[0.6875rem] text-muted-foreground/70">
        {msg("auto.features.agent.panel.components.walletcard.title")}
      </span>
    </div>
  );

  if (view.kind === "hosted") {
    const customBody = (
      <div className="space-y-1.5">
        {headline}
        {view.low && (
          <p className="text-[0.6875rem] text-muted-foreground">{msg("billing.chip.low_note")}</p>
        )}
      </div>
    );
    return <ToolCallRow call={call} summary={summary} customBody={customBody} />;
  }

  const { grant, usage } = view;

  const customBody = (
    <div className="space-y-3">
      {headline}

      <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5">
        {/* Only legacy accounts still hold a grant — hide the tile when the
            account never had one. */}
        {(grant.cents_total ?? 0) > 0 && (
          <StatTile
            label={msg("auto.features.agent.panel.components.walletcard.free_grant")}
            value={
              grant.cents_remaining == null
                ? null
                : `${fmtUsd(grant.cents_remaining)} / ${fmtUsd(grant.cents_total ?? 0)}`
            }
            valueDir="ltr"
          />
        )}
        <StatTile
          label={msg("auto.features.agent.panel.components.walletcard.paid_balance")}
          value={fmtUsd(view.paidCents)}
          valueDir="ltr"
        />
      </dl>

      {usage.length > 0 && (
        <div className="space-y-1">
          <div className="text-[0.625rem] uppercase tracking-wide text-muted-foreground/60">
            {msg("auto.features.agent.panel.components.walletcard.recent")}
          </div>
          <ul className="divide-y divide-border/40">
            {usage.slice(0, 5).map((row, idx) => (
              <li key={row.id ?? idx} className="flex items-center gap-2 py-1 text-[0.6875rem]">
                <span dir="auto" className="min-w-0 flex-1 truncate text-foreground/75">
                  {row.label ?? "—"}
                </span>
                <AmountDelta cents={row.cents ?? 0} />
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );

  return <ToolCallRow call={call} summary={summary} customBody={customBody} />;
}

function AmountDelta({ cents }: { cents: number }) {
  const positive = cents > 0;
  const text = positive ? `+${fmtUsd(cents)}` : fmtUsd(cents);
  return (
    <span
      dir="ltr"
      className="shrink-0 font-mono tabular-nums"
      style={{ color: positive ? "var(--success)" : undefined }}
    >
      {text}
    </span>
  );
}
