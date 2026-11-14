"use client";

import { formatMsg, msg } from "@/shared/lib/messages";
import { TooltipButton } from "@/shared/ui/tooltip-button";

import { CONTEXT_FULL_FROM, contextUsage } from "./context-usage";

// Codex's composer donut: 12 px box, 2 px line, a faint full track and a round-capped arc from 12 o'clock.
const SIZE = 12;
const LINE = 2;
const R = (SIZE - LINE) / 2;

/** A small ring in the composer that shows how full the chat's context window is; its tooltip gives the numbers. */
export function ContextUsageRing({
  usedTokens,
  contextWindow,
}: {
  usedTokens: number | null;
  contextWindow: number | null;
}) {
  const u = contextUsage(usedTokens, contextWindow);
  if (!u) return null;
  const full = u.usage >= CONTEXT_FULL_FROM;
  return (
    <TooltipButton
      side="top"
      align="center"
      tooltip={
        <span className="flex w-38 flex-col gap-0.5 text-center">
          <span className="text-muted-foreground">{msg("shared.agent.context_label")}</span>
          <span className={full ? "text-muted-foreground" : undefined}>
            {full
              ? formatMsg("shared.agent.context_full", { usage: u.usage })
              : formatMsg("shared.agent.context_left", { usage: u.usage, remaining: u.remaining })}
          </span>
          <span>{formatMsg("shared.agent.context_tokens", { used: u.usedK, window: u.windowK })}</span>
        </span>
      }
    >
      <span
        role="img"
        tabIndex={0}
        aria-label={formatMsg("shared.agent.context_aria", { percent: u.usage })}
        className="inline-flex size-7 shrink-0 items-center justify-center rounded-full text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <svg viewBox={`0 0 ${SIZE} ${SIZE}`} className="size-3.5" fill="none" aria-hidden>
          <circle cx={SIZE / 2} cy={SIZE / 2} r={R} stroke="currentColor" strokeWidth={LINE} opacity={0.16} />
          <circle
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={R}
            stroke="currentColor"
            strokeWidth={LINE}
            strokeLinecap="round"
            opacity={u.usage === 0 ? 0 : 1}
            pathLength={100}
            strokeDasharray={100}
            strokeDashoffset={100 - u.usage}
            // RTL keeps the arc clockwise: a fill gauge, not reading order.
            transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}
            className="transition-[stroke-dashoffset] duration-150 ease-out motion-reduce:transition-none"
          />
        </svg>
      </span>
    </TooltipButton>
  );
}
