"use client";

import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";

import { ChartBar, X } from "@/shared/ui/icons";
import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { Button } from "@/shared/ui/primitives/button";

import type { SessionStats } from "../lib/session-stats";

const EASE = [0.16, 1, 0.3, 1] as const;

/**
 * The `/session` card's open state for one chat view. A switch to another chat
 * (a new `chatKey`) closes the card, so it never shows a chat it was not opened for.
 */
export function useSessionCard(chatKey: number) {
  const [openFor, setOpenFor] = React.useState<number | null>(null);
  const show = React.useCallback(() => setOpenFor(chatKey), [chatKey]);
  const close = React.useCallback(() => setOpenFor(null), []);
  return { open: openFor === chatKey, show, close };
}

/** The `/session` card: the chat's message, tool, context, compaction and cost numbers. */
export function SessionStatsCard({ stats, onClose }: { stats: SessionStats; onClose: () => void }) {
  const reduce = useReducedMotion();
  const ref = React.useRef<HTMLElement>(null);
  // The card opens from the composer; focus on it tells a screen reader it is
  // there, and a close gives focus back to the opener, not to the page body.
  React.useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const card = ref.current;
    card?.focus();
    return () => {
      const active = document.activeElement;
      const lost = !active || active === document.body || Boolean(card?.contains(active));
      if (lost && opener?.isConnected && opener !== card) opener.focus();
    };
  }, []);
  const locale = getActiveIntlLocale();
  const count = new Intl.NumberFormat(locale);
  const percent = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 });
  const usd = new Intl.NumberFormat(locale, { style: "currency", currency: "USD" });
  const rows: Array<[string, string]> = [
    [msg("agent.session.user_messages"), count.format(stats.userMessages)],
    [msg("agent.session.assistant_messages"), count.format(stats.assistantMessages)],
    [msg("agent.session.tool_calls"), count.format(stats.toolCalls)],
    [
      msg("agent.session.context"),
      formatMsg("agent.session.context_value", {
        tokens: count.format(stats.contextTokens),
        window: count.format(stats.contextWindow),
        percent: percent.format(Math.min(1, stats.contextTokens / stats.contextWindow)),
      }),
    ],
    [msg("agent.session.compactions"), count.format(stats.compactions)],
    [msg("agent.session.cost"), usd.format(stats.cents / 100)],
  ];
  return (
    <motion.section
      ref={ref}
      tabIndex={-1}
      aria-label={msg("agent.session.title")}
      onKeyDown={(e) => {
        if (e.key !== "Escape") return;
        e.stopPropagation();
        onClose();
      }}
      initial={{ opacity: 0, y: reduce ? 0 : 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: EASE }}
      className="rounded-2xl border border-border bg-card px-4 py-3 text-sm focus:outline-none"
    >
      <div className="mb-2 flex items-center gap-2">
        <ChartBar aria-hidden className="size-4 text-muted-foreground" />
        <h3 className="flex-1 font-medium text-foreground">{msg("agent.session.title")}</h3>
        <Button
          variant="ghost"
          size="icon-xs"
          onClick={onClose}
          aria-label={msg("shared.dialog.close")}
        >
          <X className="size-3.5" />
        </Button>
      </div>
      <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            {/* The isolate keeps numbers in order; the dd keeps the page's
                direction, so the values line up at its end in RTL too. */}
            <dd className="text-end tabular-nums">
              <bdi>{value}</bdi>
            </dd>
          </div>
        ))}
      </dl>
    </motion.section>
  );
}
