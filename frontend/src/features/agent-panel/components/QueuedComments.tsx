"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { lineCommentId, type LineComment } from "@/shared/ui/agent/line-comments";
import { FileTypeIcon } from "@/shared/ui/file-tree";
import { PencilSimple, Trash } from "@/shared/ui/icons";
import { TooltipButton } from "@/shared/ui/tooltip-button";

type Props = {
  comments: readonly LineComment[];
  /** Shows the comment's file in the Changes pane. */
  onOpen: (comment: LineComment) => void;
  onRemove?: (comment: LineComment) => void;
  onEdit?: (comment: LineComment, text: string) => void;
};

const fileName = (c: LineComment) => c.path.split("/").pop() ?? c.path;

function IconAction({
  label,
  onClick,
  danger,
  children,
}: {
  label: string;
  onClick: () => void;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <TooltipButton delayDuration={250} tooltip={label}>
      <button
        type="button"
        onClick={onClick}
        aria-label={label}
        className={cn(
          "grid size-5 shrink-0 cursor-pointer place-items-center rounded text-muted-foreground transition-colors duration-150 hover:bg-accent",
          danger ? "hover:text-destructive" : "hover:text-foreground",
        )}
      >
        {children}
      </button>
    </TooltipButton>
  );
}

/** One queued comment. A click shows it in the pane; the pencil edits it in place. */
function Row({ c, onOpen, onRemove, onEdit }: { c: LineComment } & Omit<Props, "comments">) {
  const [editing, setEditing] = React.useState(false);
  const [text, setText] = React.useState(c.text);
  // Enter and Escape unmount the focused input, and its blur fires after them.
  const ended = React.useRef(false);
  const save = () => {
    if (ended.current) return;
    ended.current = true;
    const next = text.trim();
    if (next && next !== c.text) onEdit?.(c, next);
    setEditing(false);
  };
  const goTo = msg("agent.line_comments.go_to");
  return (
    <li className="group flex h-7 items-center gap-2 ps-2.5 pe-1.5 text-xs transition-colors hover:bg-foreground/[0.03]">
      <TooltipButton delayDuration={250} tooltip={goTo}>
        <button
          type="button"
          onClick={() => onOpen(c)}
          aria-label={goTo}
          dir="ltr"
          className="flex w-32 shrink-0 cursor-pointer items-center gap-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
        >
          <FileTypeIcon name={c.path} className="size-3.5 shrink-0" />
          <span className="truncate">{fileName(c)}</span>
          <span className="shrink-0 text-muted-foreground/70">:{c.line}</span>
        </button>
      </TooltipButton>
      {editing ? (
        <input
          autoFocus
          dir="auto"
          value={text}
          aria-label={msg("agent.line_comments.edit")}
          onChange={(e) => setText(e.target.value)}
          onBlur={save}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              save();
            }
            if (e.key === "Escape") {
              e.stopPropagation();
              ended.current = true;
              setText(c.text);
              setEditing(false);
            }
          }}
          className="h-5 min-w-0 flex-1 rounded border border-border bg-background px-1.5 text-xs text-foreground outline-none focus:border-primary/60"
        />
      ) : (
        <button
          type="button"
          onClick={() => onOpen(c)}
          dir="auto"
          title={c.text}
          className="min-w-0 flex-1 cursor-pointer truncate text-start text-foreground"
        >
          {c.text}
        </button>
      )}
      {!editing && (
        <span className="flex items-center gap-0.5 opacity-0 transition-opacity duration-150 focus-within:opacity-100 group-hover:opacity-100 [@media(hover:none)]:opacity-100">
          {onEdit && (
            <IconAction
              label={msg("agent.line_comments.edit")}
              onClick={() => {
                ended.current = false;
                setText(c.text);
                setEditing(true);
              }}
            >
              <PencilSimple className="size-3" aria-hidden />
            </IconAction>
          )}
          {onRemove && (
            <IconAction label={msg("agent.line_comments.delete")} onClick={() => onRemove(c)} danger>
              <Trash className="size-3" aria-hidden />
            </IconAction>
          )}
        </span>
      )}
    </li>
  );
}

/**
 * The line comments queued for the agent's next message, above the composer:
 * one panel with a row for each comment. Two rows show, and the rest scroll.
 */
export function QueuedComments({ comments, ...actions }: Props) {
  return (
    <ul
      aria-label={msg("agent.line_comments.queued")}
      className="max-h-[calc(2*1.75rem+0.25rem)] w-full overflow-y-auto overscroll-contain rounded-lg border border-border bg-muted/40 py-0.5"
    >
      {comments.map((c) => (
        <Row key={lineCommentId(c)} c={c} {...actions} />
      ))}
    </ul>
  );
}
