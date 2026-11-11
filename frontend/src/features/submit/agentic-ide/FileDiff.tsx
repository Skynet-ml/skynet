"use client";

import * as React from "react";

import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import type { LineComment } from "@/shared/ui/agent/line-comments";
import { Button } from "@/shared/ui/primitives/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";
import {
  ArrowCounterClockwise,
  CaretDown,
  CaretRight,
  DotsThree,
  FileCode,
  Plus,
} from "@/shared/ui/icons";

import {
  diffRows,
  fileStats,
  hunkStarts,
  isChanged,
  splitRows,
  type DiffRow,
  type DraftFile,
} from "./draft-changes";

export const ADDED_TONE = "text-[#5A7247] dark:text-[#adc899]";
// A 2px bar on the start side marks each changed row; the diff body is always LTR.
const ADDED_FILL =
  "bg-[color-mix(in_oklab,var(--success)_18%,transparent)] shadow-[inset_2px_0_0_var(--success)]";
const REMOVED_FILL = "bg-destructive/12 shadow-[inset_2px_0_0_var(--destructive)]";
// The empty side of a split row is hatched, so it does not read as a blank line.
const HATCH =
  "bg-[repeating-linear-gradient(135deg,transparent_0_5px,color-mix(in_oklab,var(--border)_55%,transparent)_5px_6px)]";

/** A parser error on one line of the new text. */
export interface LineMarker {
  line: number;
  message: string;
}

/** The line counts of a change, in green and red. */
export function ChangeCounts({ added, removed }: { added: number; removed: number }) {
  return (
    <span dir="ltr" className="flex shrink-0 gap-1.5 font-mono text-[0.6875rem] tabular-nums">
      <span className={ADDED_TONE}>+{added}</span>
      <span className="text-destructive">−{removed}</span>
    </span>
  );
}

function Gap({ count }: { count: number }) {
  return (
    <div className="select-none bg-muted/45 px-3 py-1 font-sans text-[0.6875rem] text-muted-foreground">
      {formatMsg("optimization.blackbox.repo.browser.folded_lines", { count })}
    </div>
  );
}

function LineNo({ n }: { n: number | null }) {
  return (
    <span className="w-9 shrink-0 select-none pe-2 text-end text-muted-foreground/60 tabular-nums">
      {n ?? ""}
    </span>
  );
}

function RevertHunk({ onRevert, disabled }: { onRevert: () => void; disabled: boolean }) {
  return (
    <button
      type="button"
      onClick={onRevert}
      disabled={disabled}
      className="absolute end-2 top-0 z-10 flex items-center gap-1 rounded border border-border bg-popover px-1.5 py-px font-sans text-[0.625rem] text-muted-foreground opacity-0 shadow-sm transition-[color,opacity] hover:text-foreground focus-visible:opacity-100 group-hover/hunk:opacity-100 disabled:group-hover/hunk:opacity-50 disabled:focus-visible:opacity-50"
    >
      <ArrowCounterClockwise className="size-3" aria-hidden />
      {msg("submit.agentic_ide.revert_hunk")}
    </button>
  );
}

// Sits over the line-number gutter; a keyboard user reaches it with Tab.
function AddComment({ line, onClick }: { line: number; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={formatMsg("submit.agentic_ide.comment.title", { line })}
      className="absolute start-0.5 top-1/2 z-10 flex size-4 -translate-y-1/2 cursor-pointer items-center justify-center rounded bg-primary-fill text-primary-foreground opacity-0 shadow-sm transition-opacity focus-visible:opacity-100 group-hover/row:opacity-100"
    >
      <Plus className="size-3" aria-hidden />
    </button>
  );
}

// The comment box follows the page's direction, inside the LTR diff.
function CommentBox({
  line,
  onCancel,
  onSubmit,
}: {
  line: number;
  onCancel: () => void;
  onSubmit: (text: string) => void;
}) {
  const [text, setText] = React.useState("");
  const send = () => {
    if (text.trim()) onSubmit(text.trim());
  };
  return (
    <div className="sticky start-0 w-full max-w-md px-3 py-2 font-sans">
      <div
        dir={getActiveDir()}
        className="rounded-lg border border-border bg-popover p-2.5 text-xs shadow-sm"
      >
        <p className="mb-1.5 text-muted-foreground">
          {formatMsg("submit.agentic_ide.comment.title", { line })}
        </p>
        <textarea
          autoFocus
          dir="auto"
          rows={2}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              onCancel();
            } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              send();
            }
          }}
          placeholder={msg("submit.agentic_ide.comment.placeholder")}
          aria-label={formatMsg("submit.agentic_ide.comment.title", { line })}
          className="w-full resize-none bg-transparent text-xs text-foreground outline-none placeholder:text-muted-foreground/60"
        />
        <div className="mt-1.5 flex justify-end gap-1.5">
          <Button size="sm" variant="ghost" className="h-6 px-2 text-xs" onClick={onCancel}>
            {msg("submit.agentic_ide.comment.cancel")}
          </Button>
          <Button size="sm" className="h-6 px-2 text-xs" disabled={!text.trim()} onClick={send}>
            {msg("submit.agentic_ide.comment.submit")}
          </Button>
        </div>
      </div>
    </div>
  );
}

// A comment that waits for the next message, under its line.
function CommentNote({ comment, onDelete }: { comment: LineComment; onDelete?: () => void }) {
  return (
    <div className="sticky start-0 w-full max-w-md px-3 py-1 font-sans">
      <div
        dir={getActiveDir()}
        className="flex items-start gap-2 rounded-lg border border-border/70 bg-muted/50 px-2.5 py-1.5 text-xs"
      >
        <p dir="auto" className="min-w-0 flex-1 whitespace-pre-wrap text-foreground">
          {comment.text}
        </p>
        {onDelete && (
          <button
            type="button"
            onClick={onDelete}
            className="shrink-0 cursor-pointer text-muted-foreground transition-colors hover:text-destructive"
          >
            {msg("submit.agentic_ide.comment.delete")}
          </button>
        )}
      </div>
    </div>
  );
}

function rowTone(type: DiffRow["type"]): string {
  return type === "add"
    ? cn(ADDED_FILL, ADDED_TONE)
    : type === "del"
      ? cn(REMOVED_FILL, "text-destructive")
      : "";
}

/**
 * One file's diff in the Changes pane: a header with its counts and its ⋯
 * menu, then the hunks, unified or side by side. Each hunk can be reverted,
 * and a parser error marks the line it points to.
 */
export function FileDiff({
  file,
  loading,
  split,
  collapsed,
  locked,
  markers,
  onToggleCollapse,
  onRevertFile,
  onRevertHunk,
  onOpenEditor,
  onCopyPath,
  comments = [],
  onComment,
  onDeleteComment,
}: {
  file: DraftFile;
  /** The file's base is still loading from the repository. */
  loading: boolean;
  split: boolean;
  collapsed: boolean;
  /** The agent is writing; reverts wait for its turn to end. */
  locked: boolean;
  markers: readonly LineMarker[];
  onToggleCollapse: () => void;
  onRevertFile: () => void;
  onRevertHunk: (hunk: number) => void;
  onOpenEditor: (line?: number) => void;
  onCopyPath: () => void;
  /** This file's comments that wait for the next message. */
  comments?: readonly LineComment[];
  /** Leave a comment on a line of the new text; absent when no chat takes comments. */
  onComment?: (line: number, text: string) => void;
  onDeleteComment?: (comment: LineComment) => void;
}) {
  // Diffs build only for the files on view, so a large draft loads per file.
  const rows = React.useMemo(
    () => (collapsed || loading ? [] : diffRows(file.before, file.after)),
    [collapsed, loading, file.before, file.after],
  );
  const stats = React.useMemo(() => fileStats(file), [file]);
  const markerAt = React.useMemo(() => new Map(markers.map((m) => [m.line, m.message])), [markers]);
  const changed = isChanged(file);

  const marked = (newNo: number | null) => (newNo !== null ? markerAt.get(newNo) : undefined);
  const pairs = React.useMemo(() => (split ? splitRows(rows) : []), [split, rows]);
  const starts = React.useMemo(() => hunkStarts(split ? pairs : rows), [split, pairs, rows]);
  const [commentAt, setCommentAt] = React.useState<number | null>(null);
  const addButton = (newNo: number | null) =>
    onComment && newNo !== null ? (
      <AddComment line={newNo} onClick={() => setCommentAt(newNo)} />
    ) : null;
  // The open comment box and the waiting comments sit under their line.
  const below = (newNo: number | null) =>
    newNo === null ? null : (
      <>
        {comments
          .filter((c) => c.line === newNo)
          .map((c, k) => (
            <CommentNote
              key={k}
              comment={c}
              onDelete={onDeleteComment && (() => onDeleteComment(c))}
            />
          ))}
        {commentAt === newNo && onComment && (
          <CommentBox
            line={newNo}
            onCancel={() => setCommentAt(null)}
            onSubmit={(text) => {
              onComment(newNo, text);
              setCommentAt(null);
            }}
          />
        )}
      </>
    );
  const withRevert = (i: number, line: React.ReactNode) => {
    const hunk = starts.get(i);
    if (hunk === undefined) return <React.Fragment key={i}>{line}</React.Fragment>;
    return (
      <div key={i} className="group/hunk relative">
        <RevertHunk onRevert={() => onRevertHunk(hunk)} disabled={locked} />
        {line}
      </div>
    );
  };

  return (
    <section data-path={file.path} className="border-b border-border/70">
      <header className="sticky top-0 z-20 flex h-9 items-center gap-2 border-b border-border/50 bg-background/95 px-3 backdrop-blur">
        <button
          type="button"
          onClick={onToggleCollapse}
          aria-expanded={!collapsed}
          className="flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 text-start"
        >
          {collapsed ? (
            <CaretRight
              className="size-3 shrink-0 text-muted-foreground rtl:rotate-180"
              aria-hidden
            />
          ) : (
            <CaretDown className="size-3 shrink-0 text-muted-foreground" aria-hidden />
          )}
          <FileCode className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          <span dir="ltr" className="truncate font-mono text-xs text-foreground">
            {file.path}
          </span>
          {markers.length > 0 && (
            <span className="size-1.5 shrink-0 rounded-full bg-destructive" aria-hidden />
          )}
        </button>
        <ChangeCounts {...stats} />
        <DropdownMenu>
          <DropdownMenuTrigger
            aria-label={msg("submit.agentic_ide.file_menu")}
            className="flex size-6 cursor-pointer items-center justify-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          >
            <DotsThree className="size-4" aria-hidden />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="min-w-40">
            <DropdownMenuItem onSelect={onCopyPath}>
              {msg("submit.agentic_ide.copy_path")}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => onOpenEditor()}>
              {msg("submit.agentic_ide.open_editor")}
            </DropdownMenuItem>
            <DropdownMenuItem disabled={!changed || locked} onSelect={onRevertFile}>
              {msg("submit.agentic_ide.revert_file")}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={onToggleCollapse}>
              {msg(collapsed ? "submit.agentic_ide.expand" : "submit.agentic_ide.collapse")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>
      {!collapsed &&
        (loading ? (
          <p className="px-3 py-3 text-xs text-muted-foreground">
            {msg("submit.agentic_ide.loading_file")}
          </p>
        ) : !changed ? (
          <p className="px-3 py-3 text-xs text-muted-foreground">
            {msg("submit.agentic_ide.no_file_changes")}
          </p>
        ) : (
          <div
            dir="ltr"
            className="overflow-x-auto font-mono text-[0.75rem]/[1.65] text-foreground/90"
          >
            {split ? (
              <div className="min-w-full">
                {pairs.map((row, i) =>
                  row.type === "gap" ? (
                    <Gap key={i} count={row.count} />
                  ) : (
                    withRevert(
                      i,
                      <>
                        <div className="grid grid-cols-2">
                          {[row.left, row.right].map((side, s) => {
                            const newNo = side && side.type !== "del" ? side.newNo : null;
                            const note = s === 1 ? marked(newNo) : undefined;
                            return (
                              <div
                                key={s}
                                title={note}
                                className={cn(
                                  "group/row relative flex min-w-0",
                                  s === 1 && "border-s border-border/50",
                                  !side && HATCH,
                                  side && side.type !== "same" && rowTone(side.type),
                                  note &&
                                    "bg-destructive/15 shadow-[inset_2px_0_0_var(--destructive)]",
                                )}
                              >
                                {s === 1 && addButton(newNo)}
                                <LineNo
                                  n={
                                    side
                                      ? s === 0
                                        ? side.type === "add"
                                          ? null
                                          : side.oldNo
                                        : newNo
                                      : null
                                  }
                                />
                                <span className="min-w-0 flex-1 whitespace-pre-wrap pe-3 [overflow-wrap:anywhere]">
                                  {side?.text || " "}
                                </span>
                              </div>
                            );
                          })}
                        </div>
                        {below(row.right ? row.right.newNo : null)}
                      </>,
                    )
                  ),
                )}
              </div>
            ) : (
              <div className="w-max min-w-full">
                {rows.map((row, i) => {
                  if (row.type === "gap") return <Gap key={i} count={row.count} />;
                  const newNo = row.type === "del" ? null : row.newNo;
                  const note = marked(newNo);
                  const line = (
                    <>
                      <div
                        title={note}
                        className={cn(
                          "group/row relative flex",
                          rowTone(row.type),
                          note && "bg-destructive/15 shadow-[inset_2px_0_0_var(--destructive)]",
                        )}
                      >
                        {addButton(newNo)}
                        <LineNo n={row.type === "add" ? null : row.oldNo} />
                        <LineNo n={newNo} />
                        <span className="w-4 shrink-0 select-none opacity-60" aria-hidden>
                          {row.type === "add" ? "+" : row.type === "del" ? "−" : ""}
                        </span>
                        <span className="whitespace-pre pe-3">{row.text || " "}</span>
                      </div>
                      {below(newNo)}
                    </>
                  );
                  return withRevert(i, line);
                })}
              </div>
            )}
          </div>
        ))}
    </section>
  );
}
