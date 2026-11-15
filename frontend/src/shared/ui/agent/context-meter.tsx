"use client";

import { formatMsg, msg } from "@/shared/lib/messages";
import { TooltipButton } from "@/shared/ui/tooltip-button";

import { CONTEXT_DOTS, CONTEXT_FULL_FROM, contextUsage, dotColor, litDots } from "./context-usage";

// A 4×4 dot matrix in a 16 px box. It fills row by row from the top start.
// RTL keeps the LTR order: it is a gauge, not reading order.
const CELLS = Array.from({ length: CONTEXT_DOTS }, (_, i) => ({
  x: (i % 4) * 4 + 2,
  y: Math.floor(i / 4) * 4 + 2,
}));

/**
 * A small dot matrix in the composer that shows how full the chat's context window
 * is; its tooltip gives the numbers. It only draws: the caller works out the
 * tokens, so the look can change without the logic.
 */
export function ContextMeter({ used, window }: { used: number | null; window: number | null }) {
  const u = contextUsage(used, window);
  if (!u) return null;
  const full = u.usage >= CONTEXT_FULL_FROM;
  const lit = litDots(u.usage);
  const color = dotColor(u.usage);
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
          <span>
            {formatMsg("shared.agent.context_tokens", { used: u.usedK, window: u.windowK })}
          </span>
        </span>
      }
    >
      <span
        role="img"
        tabIndex={0}
        aria-label={formatMsg("shared.agent.context_aria", { percent: u.usage })}
        className="inline-flex size-7 shrink-0 items-center justify-center rounded-full text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <svg viewBox="0 0 16 16" className="size-4" aria-hidden>
          {CELLS.map(({ x, y }, i) => {
            const on = i < lit;
            return (
              <circle
                key={i}
                cx={x}
                cy={y}
                r={1.4}
                fill={on ? color : "currentColor"}
                opacity={on ? 1 : 0.2}
                className="transition-opacity duration-150 ease-out motion-reduce:transition-none"
              />
            );
          })}
        </svg>
      </span>
    </TooltipButton>
  );
}
