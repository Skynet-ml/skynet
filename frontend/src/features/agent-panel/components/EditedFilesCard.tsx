"use client";

import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/primitives/button";
import { GitDiff } from "@/shared/ui/icons";

import { editTotals, type FileEdit } from "../lib/turn-edits";

const ADDED_TONE = "text-[#5A7247] dark:text-[#adc899]";

function Counts({ added, removed }: { added: number; removed: number }) {
  return (
    <span dir="ltr" className="flex shrink-0 gap-1.5 font-mono text-[0.6875rem] tabular-nums">
      <span className={ADDED_TONE}>+{added}</span>
      <span className="text-destructive">−{removed}</span>
    </span>
  );
}

/**
 * The receipt under a setup turn that edited files: "Edited N files" with
 * the line counts, one row per file, and View changes, which opens the
 * Changes pane beside the chat.
 */
export function EditedFilesCard({
  edits,
  onView,
}: {
  edits: readonly FileEdit[];
  /** Open the Changes pane, at one file when given; null when no pane is on screen. */
  onView: ((path?: string) => void) | null;
}) {
  const totals = editTotals(edits);
  return (
    <div className="mt-3 overflow-hidden rounded-lg border border-border/70 bg-card/60 text-xs">
      <div className="flex items-center gap-2.5 px-3 py-2.5">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-muted/70 text-muted-foreground">
          <GitDiff className="size-3.5" aria-hidden />
        </span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="flex items-center gap-2 font-medium text-foreground">
            {formatMsg("agent.edited_files.title", { count: edits.length })}
            <Counts {...totals} />
          </span>
        </span>
        {onView && (
          <Button size="sm" variant="outline" className="h-7" onClick={() => onView()}>
            {msg("agent.edited_files.view")}
          </Button>
        )}
      </div>
      <ul className="border-t border-border/60">
        {edits.map((edit) => (
          <li key={edit.path} className="border-b border-border/40 last:border-b-0">
            <button
              type="button"
              disabled={!onView}
              onClick={() => onView?.(edit.path)}
              className={cn(
                "flex w-full items-center justify-between gap-3 px-3 py-2 text-start transition-colors",
                onView && "cursor-pointer hover:bg-muted/40",
              )}
            >
              <span dir="ltr" className="truncate font-mono text-[0.6875rem] text-foreground">
                {edit.path}
              </span>
              <Counts added={edit.added} removed={edit.removed} />
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
