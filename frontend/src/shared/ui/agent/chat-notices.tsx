"use client";

import { formatMsg } from "@/shared/lib/messages";
import { Cube } from "@/shared/ui/icons";

// The step row's quiet line: Codex draws the notice as an inline status item in the transcript.
const LINE = "flex w-full min-w-0 items-center gap-2 py-1 text-[0.8125rem] text-muted-foreground";

/** The line before a turn that runs on another model than the turn before it. */
export function ModelChangedNotice({ fromModel, toModel }: { fromModel: string; toModel: string }) {
  return (
    <div className={LINE}>
      <Cube className="size-3.5 shrink-0" aria-hidden />
      <span dir="auto" className="min-w-0 truncate">
        {/* Isolates keep a name that ends in punctuation whole inside an RTL sentence. */}
        {formatMsg("shared.agent.model_changed", {
          from: `⁨${fromModel}⁩`,
          to: `⁨${toModel}⁩`,
        })}
      </span>
    </div>
  );
}
