"use client";

import * as React from "react";

import { formatMsg, msg } from "@/shared/lib/messages";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/shared/ui/primitives/tooltip";

/** "2 denied actions" at the end of a turn's header, with why on hover (SKY-706); nothing at 0. */
export function DeniedCount({ count }: { count: number }) {
  if (count <= 0) return null;
  return (
    <TooltipProvider delayDuration={250}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            tabIndex={0}
            className="ms-auto shrink-0 rounded-sm text-[0.8125rem] tabular-nums text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {formatMsg("shared.agent.denied_actions", { count })}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-64 text-center">
          {msg("shared.agent.denied_actions_tooltip")}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
