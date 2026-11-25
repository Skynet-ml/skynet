"use client";

import { GitFork } from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { Button } from "@/shared/ui/primitives/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/shared/ui/primitives/tooltip";

/** The "Fork from here" icon button in a message's action row. */
export function ForkButton({ onFork }: { onFork: () => void }) {
  // Its own provider: the user bubble renders it outside the actions row.
  return (
    <TooltipProvider>
      <Tooltip disableHoverableContent>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon-xs"
            onClick={onFork}
            aria-label={msg("shared.agent.fork")}
          >
            <GitFork className="size-3.5" />
          </Button>
        </TooltipTrigger>
        {/* Screen readers get the hint only; the title repeats the button's label. */}
        <TooltipContent
          side="top"
          dir={getActiveDir()}
          aria-label={msg("shared.agent.fork_hint")}
          className="text-start text-pretty"
        >
          <p className="font-semibold">{msg("shared.agent.fork")}</p>
          <p className="font-normal text-muted-foreground">{msg("shared.agent.fork_hint")}</p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
