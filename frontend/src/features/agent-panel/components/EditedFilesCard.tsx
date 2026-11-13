"use client";

import { useId, useState, type ReactNode } from "react";

import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { CaretDown, PlusMinus } from "@/shared/ui/icons";

import { editTotals, type FileEdit } from "../lib/turn-edits";

const ADDED_TONE = "text-[#5A7247] dark:text-[#adc899]";
// Above this many files, the rest fold under a "Show N more files" row.
const SHOWN = 3;

function Counts({ added, removed }: { added: number; removed: number }) {
  return (
    <span dir="ltr" className="flex shrink-0 gap-1.5 tabular-nums">
      <span className={ADDED_TONE}>+{added}</span>
      <span className="text-destructive">−{removed}</span>
    </span>
  );
}

// A row opens the Changes pane at its file when a pane is on screen; else it is plain text.
function Clickable({
  onClick,
  className,
  children,
}: {
  onClick: (() => void) | null;
  className: string;
  children: ReactNode;
}) {
  if (!onClick) return <div className={className}>{children}</div>;
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        className,
        "w-full cursor-pointer text-start transition-colors hover:bg-muted/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40",
      )}
    >
      {children}
    </button>
  );
}

/**
 * The card under a setup turn that edited files. One file shows "Edited
 * <file>" with its line counts; more files show "Edited N files" with the
 * totals and one row per file with its counts. Past three files, the rest fold
 * under a row that expands them. The counts are that turn's own changes.
 */
export function EditedFilesCard({
  edits,
  onView,
}: {
  edits: readonly FileEdit[];
  /** Open the Changes pane at a file, marking the turn's files; null when no pane is on screen. */
  onView: ((path?: string, paths?: readonly string[]) => void) | null;
}) {
  const paths = edits.map((edit) => edit.path);
  const open = (path: string) => (onView ? () => onView(path, paths) : null);
  const single = edits.length === 1 ? edits[0]! : null;
  const [expanded, setExpanded] = useState(false);
  const moreId = useId();
  const hidden = edits.length - SHOWN;
  const row = (edit: FileEdit) => (
    <li key={edit.path}>
      <Clickable
        onClick={open(edit.path)}
        className="flex items-center justify-between gap-3 px-3 py-2 text-sm"
      >
        <span dir="ltr" className="min-w-0 truncate text-foreground">
          {edit.path}
        </span>
        <Counts added={edit.added} removed={edit.removed} />
      </Clickable>
    </li>
  );
  const header = (
    <>
      <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted text-foreground">
        <PlusMinus className="size-4.5" aria-hidden />
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="block truncate text-sm font-medium text-foreground">
          {single
            ? // The isolates keep a left-to-right file name whole inside right-to-left copy.
              formatMsg("agent.edited_files.edited_one", { file: `\u2068${single.path}\u2069` })
            : formatMsg("agent.edited_files.edited_many", { count: edits.length })}
        </span>
        <span className="text-xs">
          <Counts {...(single ?? editTotals(edits))} />
        </span>
      </span>
    </>
  );
  return (
    <div className="ide-rise-in mt-3 overflow-hidden rounded-xl border border-border bg-card">
      {single ? (
        <Clickable onClick={open(single.path)} className="flex items-center gap-2.5 p-2">
          {header}
        </Clickable>
      ) : (
        <>
          <div className="flex items-center gap-2.5 p-2">{header}</div>
          <ul className="border-t border-border">{edits.slice(0, SHOWN).map(row)}</ul>
          {hidden > 0 && (
            <>
              {/* Inert while folded, so Tab skips the rows no one can see. */}
              <div
                id={moreId}
                inert={!expanded}
                className={cn(
                  "grid transition-[grid-template-rows,opacity] duration-200 ease-out motion-reduce:transition-none",
                  expanded ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
                )}
              >
                <ul className="min-h-0 overflow-hidden">{edits.slice(SHOWN).map(row)}</ul>
              </div>
              <button
                type="button"
                aria-expanded={expanded}
                aria-controls={moreId}
                onClick={() => setExpanded((was) => !was)}
                className="flex w-full cursor-pointer items-center gap-1.5 border-t border-border bg-muted/30 px-3 py-2 text-start text-sm text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40"
              >
                {expanded
                  ? msg("agent.edited_files.show_fewer")
                  : formatMsg("agent.edited_files.show_more", { count: hidden })}
                <CaretDown
                  className={cn(
                    "size-3.5 transition-transform duration-200 ease-out motion-reduce:transition-none",
                    expanded && "rotate-180",
                  )}
                  aria-hidden
                />
              </button>
            </>
          )}
        </>
      )}
    </div>
  );
}
