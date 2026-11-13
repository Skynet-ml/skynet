"use client";

import * as React from "react";
import { toast } from "react-toastify";

import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import type { LineComment } from "@/shared/ui/agent/line-comments";
import { DiffView, type LineSlotProps, type LineSlots } from "@/shared/ui/repo-files/repo-files";
import { TooltipButton } from "@/shared/ui/tooltip-button";
import { Button } from "@/shared/ui/primitives/button";
import { Plus, X } from "@/shared/ui/icons";

import { body, draftRows, lineMap, type DraftFile } from "./draft-changes";

// The line that holds the file's tab stop. A context, so a move of the tab
// stop redraws the lines' controls, not every line of the file.
const ActiveLine = React.createContext<number | null>(null);

// Sits over the line-number gutter; a keyboard user reaches it with Tab. Only
// the line that holds the file's tab stop puts its button in the tab order.
function AddComment({ line, onClick }: { line: number; onClick: () => void }) {
  const tabStop = React.useContext(ActiveLine) === line;
  // No tooltip: one for each line of a whole file costs a provider each, and
  // the label already says the same. A hidden button takes no click, and a
  // screen with no hover shows it.
  return (
    <button
      type="button"
      tabIndex={tabStop ? 0 : -1}
      onClick={onClick}
      aria-label={formatMsg("submit.agentic_ide.comment.title", { line })}
      className="pointer-events-none absolute start-0.5 top-1/2 z-10 flex size-4 -translate-y-1/2 cursor-pointer items-center justify-center rounded bg-primary-fill text-primary-foreground opacity-0 shadow-sm transition-opacity focus-visible:pointer-events-auto focus-visible:opacity-100 group-hover/row:pointer-events-auto group-hover/row:opacity-100 [@media(hover:none)]:pointer-events-auto [@media(hover:none)]:opacity-100"
    >
      <Plus className="size-3" aria-hidden />
    </button>
  );
}

// The comment box follows the page's direction, inside the LTR diff.
function CommentBox({
  line,
  initial,
  onType,
  onCancel,
  onSubmit,
  ref,
}: {
  line: number;
  /** The text the box starts with; the parent keeps the typed text, so a remount keeps it. */
  initial: string;
  onType: (text: string) => void;
  onCancel: () => void;
  onSubmit: (text: string) => void;
  ref: React.Ref<HTMLTextAreaElement>;
}) {
  const [text, setText] = React.useState(initial);
  const send = () => {
    if (text.trim()) onSubmit(text.trim());
  };
  return (
    <div className="ide-rise-in sticky start-0 w-full max-w-md px-3 py-2 font-sans">
      <div
        dir={getActiveDir()}
        className="rounded-lg border border-border bg-popover p-2.5 text-xs shadow-sm"
      >
        <p className="mb-1.5 text-muted-foreground">
          {formatMsg("submit.agentic_ide.comment.title", { line })}
        </p>
        <textarea
          ref={ref}
          dir="auto"
          rows={2}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            onType(e.target.value);
          }}
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

function CommentNote({ comment, onDelete }: { comment: LineComment; onDelete?: () => void }) {
  return (
    <div className="ide-rise-in sticky start-0 w-full max-w-md px-3 py-1 font-sans">
      <div
        dir={getActiveDir()}
        className="flex items-start gap-2 rounded-lg border border-border/70 bg-muted/50 px-2.5 py-1.5 text-xs"
      >
        <p dir="auto" className="min-w-0 flex-1 whitespace-pre-wrap text-foreground">
          {comment.text}
        </p>
        {onDelete && (
          <TooltipButton tooltip={msg("submit.agentic_ide.comment.delete")}>
            <button
              type="button"
              onClick={onDelete}
              aria-label={msg("submit.agentic_ide.comment.delete")}
              className="-me-1 flex size-5 shrink-0 cursor-pointer items-center justify-center rounded text-muted-foreground transition-colors hover:bg-muted hover:text-destructive"
            >
              <X className="size-3" aria-hidden />
            </button>
          </TooltipButton>
        )}
      </div>
    </div>
  );
}

// Blur saves, as a code editor does, so a click on another line keeps the typed text.
function LineEditor({
  text,
  initial,
  onType,
  line,
  readOnly,
  onDone,
  ref,
}: {
  text: string;
  /** The text the editor starts with; the parent keeps the typed text, so a remount keeps it. */
  initial: string;
  onType: (value: string) => void;
  line: number;
  /** The file is locked: the text stays, and it can be finished again once the lock ends. */
  readOnly: boolean;
  /**
   * The new text, null to delete the line, or undefined to keep it as it was;
   * and whether a key closed the editor, so focus goes back to the line.
   */
  onDone: (next: string | null | undefined, byKey: boolean) => void;
  ref: React.Ref<HTMLTextAreaElement>;
}) {
  // The text lives here, so a key press redraws the editor, not every line of the file.
  const [value, setValue] = React.useState(initial);
  const done = React.useRef(false);
  const finish = (next: string | null | undefined, byKey = true) => {
    if (done.current) return;
    if (!readOnly) done.current = true;
    onDone(next === text ? undefined : next, byKey);
  };
  return (
    <textarea
      ref={ref}
      // The arrow keys of the lines around it reach the open editor too.
      data-line={line}
      readOnly={readOnly}
      spellCheck={false}
      rows={value.split("\n").length}
      value={value}
      onFocus={(e) => e.currentTarget.setSelectionRange(value.length, value.length)}
      onChange={(e) => {
        setValue(e.target.value);
        onType(e.target.value);
      }}
      onBlur={() => finish(value, false)}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          finish(undefined);
        } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
          e.preventDefault();
          finish(value);
        } else if (e.key === "Backspace" && value === "" && !e.repeat) {
          // A held Backspace that empties the line stops there; a new press deletes it.
          e.preventDefault();
          finish(null);
        }
      }}
      aria-label={formatMsg("submit.agentic_ide.edit_line", { line })}
      className="ide-rise-in min-w-0 flex-1 resize-none overflow-y-auto whitespace-pre-wrap rounded-sm bg-background pe-3 text-foreground outline-none ring-1 ring-primary/60 [field-sizing:content] [overflow-wrap:anywhere]"
    />
  );
}

/** A line of the new text: a click or Enter edits it in place. */
function NewLine({
  line,
  text,
  code,
  change,
  className,
  editable,
  onOpen,
  onFocus,
  onMove,
}: LineSlotProps & {
  editable: boolean;
  onOpen: () => void;
  onFocus: () => void;
  /** Focus the next or previous line; false when there is none to focus. */
  onMove: (down: boolean, from: HTMLElement) => boolean;
}) {
  const tabStop = React.useContext(ActiveLine) === line;
  return (
    <span
      className={cn(
        className,
        "rounded-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        editable && "cursor-text",
      )}
      role="button"
      aria-disabled={!editable || undefined}
      tabIndex={tabStop ? 0 : -1}
      data-line={line}
      onFocus={onFocus}
      // The code stays the name a screen reader reads and the action is its
      // description. A blank line has no text to read, so the action is its
      // name, after the change that the label hides.
      {...(text.trim()
        ? { "aria-description": formatMsg("submit.agentic_ide.edit_line", { line }) }
        : {
            "aria-label": [change, formatMsg("submit.agentic_ide.edit_line", { line })]
              .filter(Boolean)
              .join(", "),
          })}
      onClick={() => {
        // A drag selects text to copy; it does not edit. A double-click opens
        // the editor on its first click, and the editor holds the same text.
        if (window.getSelection()?.toString() || !editable) return;
        onOpen();
      }}
      onKeyDown={(e) => {
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          // With no line to go to, the key scrolls the view as usual.
          if (onMove(e.key === "ArrowDown", e.currentTarget)) e.preventDefault();
          return;
        }
        if (!editable || (e.key !== "Enter" && e.key !== " ")) return;
        e.preventDefault();
        onOpen();
      }}
    >
      {code}
    </span>
  );
}

const NO_COMMENTS: readonly LineComment[] = [];

/**
 * The open file in the Changes pane: its changes, or its text when it has
 * none. A click on a line of the new text edits it in place, and a line can
 * take a comment for the next message.
 */
export function FileDiff({
  file,
  loading,
  onEditLine,
  comments = NO_COMMENTS,
  onComment,
  onDeleteComment,
}: {
  file: DraftFile;
  /** The file's base is still loading from the repository. */
  loading: boolean;
  /** Write one line of the new text (null deletes it); absent while the file cannot be edited. */
  onEditLine?: (file: DraftFile, line: number, text: string | null) => void;
  /** This file's comments that wait for the next message. */
  comments?: readonly LineComment[];
  /** Leave a comment on a line of the new text; absent when no chat takes comments. */
  onComment?: (path: string, line: number, text: string) => void;
  onDeleteComment?: (comment: LineComment) => void;
}) {
  const rows = React.useMemo(() => draftRows(file.before, file.after), [file.before, file.after]);
  const [held, setHeld] = React.useState<{ next: string | null } | null>(null);
  const [editAt, setEditAt] = React.useState<number | null>(null);
  // A write that makes the file shorter can leave a comment past its end; it
  // shows on the last line.
  const lastLine = React.useMemo(() => body(file.after).split("\n").length, [file.after]);
  const [commentAt, setCommentAt] = React.useState<number | null>(null);
  // A fold never takes a commented line, the open comment box or the open
  // editor, so each shows under or on its line.
  const keep = React.useMemo(() => {
    const lines = new Set(comments.map((c) => Math.min(c.line, lastLine)));
    if (commentAt !== null) lines.add(commentAt);
    if (editAt !== null) lines.add(editAt);
    return lines;
  }, [comments, lastLine, commentAt, editAt]);
  // The typed texts; kept here, so a remount of the editor or the box keeps them.
  const commentText = React.useRef("");
  const editValue = React.useRef("");
  // The line a key closed the editor on; it takes focus back once it renders.
  const refocus = React.useRef<number | null>(null);
  // A lock (an agent turn) keeps the editor and its text, read only. A save
  // that comes during the lock waits here and goes through when the lock ends.
  // When the text changes under it, the editor stays only if the line count
  // and every line up to its line stay the same; otherwise the number may name
  // another line, so the edit is dropped and a toast tells the user.
  const [editedAfter, setEditedAfter] = React.useState(file.after);
  // The typed text is compared in the effect: render must not read a ref.
  const [dropped, setDropped] = React.useState<{ line: number; was: string; held: boolean } | null>(
    null,
  );
  const editor = React.useRef<HTMLTextAreaElement | null>(null);
  // The editor takes the focus when it mounts only after the user opens it, or
  // when it remounts (an agent write) while it has the focus. An agent write
  // must not take the focus from the chat input.
  const focusEditor = React.useRef(false);
  const commentBox = React.useRef<HTMLTextAreaElement | null>(null);
  const focusComment = React.useRef(false);
  if (editAt !== null && editedAfter !== file.after) {
    // A change of only the final newline moves no line.
    const was = body(editedAfter).split("\n");
    const now = body(file.after).split("\n");
    // A line added or removed anywhere, or a change at or above the line, can
    // move another line with the same text (a lone "}") to its number.
    if (was.length !== now.length || was.slice(0, editAt).some((l, i) => l !== now[i])) {
      setDropped({ line: editAt, was: was[editAt - 1]?.replace(/\r$/, "") ?? "", held: !!held });
      setEditAt(null);
      setHeld(null);
    }
  }
  // The arrow keys move along the new text's lines, and only one line is a tab
  // stop, so Tab passes the lines in one press. The active line's add-comment
  // button, each fold button and the comment controls are tab stops too.
  const [active, setActive] = React.useState<number | null>(null);
  // DiffView draws no line until it knows its width, so the tab stop is picked
  // again once it does.
  const [measured, setMeasured] = React.useState(false);
  // An open comment box and the tab stop move with their lines, so a comment
  // goes to the line it was opened on and the focus stays on its text.
  if (editedAfter !== file.after) {
    const map = lineMap(editedAfter, file.after);
    if (commentAt !== null) setCommentAt(map(commentAt));
    if (active !== null) setActive(map(active));
    setEditedAfter(file.after);
  }
  React.useEffect(() => {
    if (dropped === null) return;
    setDropped(null);
    if (dropped.held || editValue.current !== dropped.was) {
      toast(formatMsg("submit.agentic_ide.edit_dropped", { line: dropped.line }));
    }
  }, [dropped]);
  React.useEffect(() => {
    if (!held || !onEditLine || editAt === null) return;
    if (document.activeElement && document.activeElement === editor.current) {
      refocus.current = editAt;
    }
    setHeld(null);
    setEditAt(null);
    onEditLine(file, editAt, held.next);
  });
  const section = React.useRef<HTMLElement>(null);
  // The ref detach does not say which line's editor closed, so the last commit's line is kept.
  const openLine = React.useRef<number | null>(null);
  React.useLayoutEffect(() => {
    // An editor that closes with the focus in it (a key, or an agent write
    // that drops the edit) gives the focus to its line, so it does not fall to
    // the page. A line past the new end gives it to the last line.
    if (editAt === null && openLine.current !== null && focusEditor.current && !editor.current) {
      focusEditor.current = false;
      refocus.current ??= Math.min(openLine.current, lastLine);
    }
    openLine.current = editAt;
    // A line that does not render in this commit (deleted, or folded into a
    // gap) drops the request, so a later render cannot take the focus from
    // somewhere else.
    const line = refocus.current;
    refocus.current = null;
    if (line !== null) {
      section.current?.querySelector<HTMLElement>(`span[data-line="${line}"]`)?.focus();
    }
    // Rows of a new file are keyed by line number, so after an agent write the
    // focused row can show another line. The focus goes back to the tab stop's text.
    const at = document.activeElement;
    if (
      line === null &&
      active !== null &&
      at instanceof HTMLElement &&
      at.matches("span[data-line]") &&
      section.current?.contains(at) &&
      at.dataset.line !== String(active)
    ) {
      section.current.querySelector<HTMLElement>(`span[data-line="${active}"]`)?.focus();
    }
    // The fold decides which lines draw; with the tab stop's line folded
    // away, the first drawn line takes it.
    const drawn = [...(section.current?.querySelectorAll<HTMLElement>("[data-line]") ?? [])];
    if (drawn.length > 0 && !drawn.some((el) => el.dataset.line === String(active))) {
      setActive(Number(drawn[0]!.dataset.line));
    }
    // Every state that closes the editor or the comment box, sets a focus
    // request, or changes which lines draw.
  }, [loading, rows, keep, editAt, held, commentAt, comments, lastLine, active, measured]);

  const attachEditor = React.useCallback((el: HTMLTextAreaElement | null) => {
    if (el) {
      editor.current = el;
      if (focusEditor.current) el.focus();
      focusEditor.current = false;
    } else {
      // React detaches the ref before it removes the node, so the focus is still on it.
      if (editor.current && document.activeElement === editor.current) {
        focusEditor.current = true;
      }
      editor.current = null;
    }
  }, []);
  const attachComment = React.useCallback((el: HTMLTextAreaElement | null) => {
    if (el) {
      commentBox.current = el;
      if (focusComment.current) el.focus();
      focusComment.current = false;
    } else {
      if (commentBox.current && document.activeElement === commentBox.current) {
        focusComment.current = true;
      }
      commentBox.current = null;
    }
  }, []);
  const notesByLine = React.useMemo(() => {
    const byLine = new Map<number, LineComment[]>();
    for (const c of comments) {
      const line = Math.min(c.line, lastLine);
      byLine.set(line, [...(byLine.get(line) ?? []), c]);
    }
    return byLine;
  }, [comments, lastLine]);
  // Stable while only the tab stop moves, so the arrow keys do not redraw every line.
  const slots = React.useMemo<LineSlots>(() => {
    const open = (line: number, text: string) => {
      editValue.current = text.replace(/\r$/, "");
      setEditAt(line);
      focusEditor.current = true;
    };
    const done = (line: number, next: string | null | undefined, byKey: boolean) => {
      if (byKey) refocus.current = line;
      // During a lock a save waits; leaving without one closes the editor.
      if (!onEditLine) {
        if (next !== undefined) return setHeld({ next });
        setHeld(null);
        return setEditAt(null);
      }
      setEditAt(null);
      if (next !== undefined) onEditLine(file, line, next);
    };
    const move = (down: boolean, from: HTMLElement) => {
      const lines = [...(section.current?.querySelectorAll<HTMLElement>("[data-line]") ?? [])];
      const el = lines[lines.indexOf(from) + (down ? 1 : -1)];
      el?.focus();
      return !!el;
    };
    const closeComment = (line: number, text?: string) => {
      if (text !== undefined) onComment?.(file.path, line, text);
      // The box leaves the page, so the focus goes back to its line.
      refocus.current = line;
      setCommentAt(null);
    };
    return {
      line: (props) =>
        editAt === props.line ? (
          <LineEditor
            // A textarea turns a lone CR into a line break; replaceLine puts it back.
            text={props.text.replace(/\r$/, "")}
            initial={editValue.current}
            onType={(value) => (editValue.current = value)}
            line={props.line}
            readOnly={!onEditLine}
            ref={attachEditor}
            onDone={(next, byKey) => done(props.line, next, byKey)}
          />
        ) : (
          <NewLine
            {...props}
            editable={!!onEditLine}
            onOpen={() => open(props.line, props.text)}
            onFocus={() => setActive(props.line)}
            onMove={move}
          />
        ),
      gutter: onComment
        ? (line) => (
            <AddComment
              line={line}
              onClick={() => {
                commentText.current = "";
                setCommentAt(line);
                focusComment.current = true;
              }}
            />
          )
        : undefined,
      measured: () => setMeasured(true),
      below: (line) => (
        <>
          {(notesByLine.get(line) ?? NO_COMMENTS).map((c, k) => (
            <CommentNote
              key={k}
              comment={c}
              onDelete={
                onDeleteComment
                  ? () => {
                      // The note leaves the page, so the focus goes back to its line.
                      refocus.current = Math.min(c.line, lastLine);
                      onDeleteComment(c);
                    }
                  : undefined
              }
            />
          ))}
          {commentAt === line && onComment && (
            <CommentBox
              line={line}
              initial={commentText.current}
              onType={(text) => (commentText.current = text)}
              ref={attachComment}
              onCancel={() => closeComment(line)}
              onSubmit={(text) => closeComment(line, text)}
            />
          )}
        </>
      ),
    };
  }, [
    file,
    onEditLine,
    onComment,
    onDeleteComment,
    editAt,
    commentAt,
    notesByLine,
    lastLine,
    attachEditor,
    attachComment,
  ]);

  if (loading) {
    return (
      <p className="px-1 py-3 text-xs text-muted-foreground">
        {msg("submit.agentic_ide.loading_file")}
      </p>
    );
  }
  return (
    <section
      ref={section}
      data-path={file.path}
      aria-label={file.path}
      className="flex min-h-0 flex-1 flex-col"
    >
      {/* With the tree hidden, nothing else names the open file. */}
      <p
        dir="ltr"
        title={file.path}
        className="mb-1.5 shrink-0 truncate px-1 font-mono text-[0.6875rem] text-muted-foreground"
      >
        {file.path}
      </p>
      <ActiveLine value={active}>
        <DiffView
          path={file.path}
          rows={rows}
          before={body(file.before)}
          after={body(file.after)}
          keep={keep}
          className="max-h-none min-h-0 flex-1"
          slots={slots}
        />
      </ActiveLine>
    </section>
  );
}
