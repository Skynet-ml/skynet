"use client";

import { formatMsg, msg } from "@/shared/lib/messages";
import { Cube, Info } from "@/shared/ui/icons";
import { TooltipButton } from "@/shared/ui/tooltip-button";

// The step row's quiet line: Codex draws the notice as an inline status item in the transcript.
const LINE = "flex w-full min-w-0 items-center gap-2 py-1 text-[0.8125rem] text-muted-foreground";

/** The line before a turn that runs on another model than the turn before it; its info icon warns about the switch. */
export function ModelChangedNotice({ fromModel, toModel }: { fromModel: string; toModel: string }) {
  return (
    <div className={LINE}>
      <Cube className="size-3.5 shrink-0" aria-hidden />
      <span dir="auto" className="min-w-0 truncate">
        {/* Isolates keep a name that ends in punctuation whole inside an RTL sentence. */}
        {formatMsg("shared.agent.model_changed", {
          from: `\u2068${fromModel}\u2069`,
          to: `\u2068${toModel}\u2069`,
        })}
      </span>
      <TooltipButton
        side="top"
        align="center"
        tooltip={
          <span className="flex max-w-64 flex-col text-center">
            <span>{msg("shared.agent.model_changed_warning_1")}</span>
            <span>{msg("shared.agent.model_changed_warning_2")}</span>
          </span>
        }
      >
        <span
          role="img"
          tabIndex={0}
          aria-label={msg("shared.agent.model_changed_warning_label")}
          className="inline-flex size-5 shrink-0 items-center justify-center rounded-full outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Info className="size-3" aria-hidden />
        </span>
      </TooltipButton>
    </div>
  );
}
