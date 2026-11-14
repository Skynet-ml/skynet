import * as React from "react";

import { useCodeAppearance } from "@/shared/lib/code-appearance";
import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { useSession } from "next-auth/react";
import { CaretDown, Check, CircleNotch, PencilSimple, Plus, Trash, X } from "@/shared/ui/icons";
import { commandKey, TooltipButton } from "@/shared/ui/tooltip-button";
import { useLinkedAccount } from "@/shared/hooks/use-linked-account";

import { FOLDER_ICON, FOLDER_OPEN_ICON, fileIcon, type FileIcon } from "@/shared/ui/file-icons";
import {
  baseName,
  diffBlocks,
  diffLines,
  dirName,
  highlightLine,
  stats,
  statusOf,
  type DiffLine,
  type FileStatus,
  type RepoFile,
  type TreeNode,
} from "./lib";

// ---------- small atoms ----------

/** Material Icon Theme glyph, the pack the product already ships (shared/ui/file-icons on staging). */
function PackIcon({ icon, className }: { icon: FileIcon; className?: string }) {
  const cls = cn("size-4 shrink-0", className);
  if (!icon.lightSrc) return <img src={icon.src} alt="" aria-hidden className={cls} />;
  return (
    <>
      <img src={icon.lightSrc} alt="" aria-hidden className={cn(cls, "dark:hidden")} />
      <img src={icon.src} alt="" aria-hidden className={cn(cls, "hidden dark:block")} />
    </>
  );
}

export function FileGlyph({ path, className }: { path: string; className?: string }) {
  return <PackIcon icon={fileIcon(baseName(path))} className={className} />;
}

function FolderGlyph({ open }: { open: boolean }) {
  return <PackIcon icon={open ? FOLDER_OPEN_ICON : FOLDER_ICON} />;
}

const STATUS_TONE: Record<Exclude<FileStatus, "unchanged">, string> = {
  added: "text-[var(--success)]",
  modified: "text-[var(--warning)]",
};
const STATUS_LETTER: Record<Exclude<FileStatus, "unchanged">, string> = {
  added: "A",
  modified: "M",
};

/** The status's name, for a tooltip. */
export function statusName(status: Exclude<FileStatus, "unchanged">): string {
  return msg(`submit.agentic_ide.review.status.${status}`);
}

/** Git-style one-letter status, the convention engineers already read. */
export function StatusLetter({ status }: { status: FileStatus }) {
  if (status === "unchanged") return null;
  return (
    <span
      className={cn("w-3 text-center font-mono text-[11px] font-semibold", STATUS_TONE[status])}
      title={statusName(status)}
    >
      {STATUS_LETTER[status]}
    </span>
  );
}

export function Counts({
  added,
  removed,
  className,
}: {
  added: number;
  removed: number;
  className?: string;
}) {
  return (
    <span
      dir="ltr"
      className={cn("inline-flex gap-1.5 font-mono text-[11px] tabular-nums", className)}
    >
      {added > 0 && <span className="text-[var(--success)]">+{added}</span>}
      {removed > 0 && <span className="text-[var(--danger)]">−{removed}</span>}
    </span>
  );
}

export function FileCounts({ file }: { file: RepoFile }) {
  return <Counts {...stats(file)} />;
}

export function totals(files: RepoFile[]) {
  return files.reduce(
    (sum, f) => {
      const s = stats(f);
      return { added: sum.added + s.added, removed: sum.removed + s.removed };
    },
    { added: 0, removed: 0 },
  );
}

/** Path with a quiet folder part and a strong file name. */
export function PathLabel({ path, className }: { path: string; className?: string }) {
  const dir = dirName(path);
  return (
    <span className={cn("min-w-0 truncate", className)} dir="ltr">
      {dir && <span className="text-muted-foreground">{dir}/</span>}
      <span className="font-medium text-foreground">{baseName(path)}</span>
    </span>
  );
}

// ---------- tree ----------

/** The folders that hold a changed file, found in one walk of the tree. */
function changedFolders(root: TreeNode): Set<string> {
  const out = new Set<string>();
  const walk = (node: TreeNode): boolean => {
    if (node.file) return statusOf(node.file) !== "unchanged";
    let changed = false;
    for (const child of node.children ?? []) if (walk(child)) changed = true;
    if (changed) out.add(node.path);
    return changed;
  };
  walk(root);
  return out;
}

/** Smooth height reveal: rows animate from 0fr to 1fr; folded content leaves the tab order. */
function Reveal({
  open,
  children,
  className,
}: {
  open: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      inert={!open}
      className={cn(
        "grid transition-[grid-template-rows,opacity] duration-200 ease-out motion-reduce:transition-none",
        open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
        className,
      )}
    >
      <div className="min-h-0 overflow-hidden">{children}</div>
    </div>
  );
}

/** A caret that points down when open and to the reading direction when shut. */
export function Caret({ open, className }: { open: boolean; className?: string }) {
  return (
    <CaretDown
      aria-hidden
      className={cn(
        "size-3 transition-transform duration-200 ease-out motion-reduce:transition-none",
        !open && "-rotate-90 rtl:rotate-90",
        className,
      )}
    />
  );
}

export function FileTree({
  root,
  selected,
  onSelect,
  touched,
}: {
  root: TreeNode;
  selected: string | null;
  onSelect: (path: string) => void;
  touched?: ReadonlySet<string>;
}) {
  const changedDirs = React.useMemo(() => changedFolders(root), [root]);
  // A folder with a change starts open and any other starts shut; a click flips it.
  const [flipped, setFlipped] = React.useState<Set<string>>(() => new Set());
  const level = (node: TreeNode, depth: number): React.ReactNode =>
    (node.children ?? []).map((child) => {
      if (child.children) {
        const changed = changedDirs.has(child.path);
        const open = changed !== flipped.has(child.path);
        return (
          <div key={child.path} className="flex flex-col gap-px">
            <button
              type="button"
              style={{ paddingInlineStart: 8 + depth * 14 }}
              onClick={() =>
                setFlipped((prev) => {
                  const next = new Set(prev);
                  if (next.has(child.path)) next.delete(child.path);
                  else next.add(child.path);
                  return next;
                })
              }
              className={cn(
                "flex w-full items-center gap-1.5 rounded-md pe-2 text-start text-[13px] text-foreground/80 transition-colors hover:bg-accent/70",
                "h-6",
              )}
              aria-expanded={open}
            >
              <Caret open={open} className="text-muted-foreground" />
              <FolderGlyph open={open} />
              <span className="truncate" dir="ltr">
                {child.name}
              </span>
              <span
                className={cn(
                  "ms-auto size-1.5 rounded-full bg-[var(--warning)] transition-opacity duration-200",
                  changed && !open ? "opacity-100" : "opacity-0",
                )}
                aria-hidden
              />
            </button>
            {/* A shut folder renders no rows, so a large repository stays fast. */}
            <Reveal open={open}>
              <div className="flex flex-col gap-px">{open && level(child, depth + 1)}</div>
            </Reveal>
          </div>
        );
      }
      if (!child.file) return null;
      const status = statusOf(child.file);
      const active = selected === child.path;
      return (
        <button
          key={child.path}
          type="button"
          style={{ paddingInlineStart: 8 + depth * 14 + 18 }}
          onClick={() => onSelect(child.path)}
          aria-current={active ? "true" : undefined}
          className={cn(
            "group flex w-full items-center gap-1.5 rounded-md pe-2 text-start text-[13px] transition-colors duration-150",
            "h-6",
            active ? "bg-accent text-foreground" : "text-foreground/85 hover:bg-accent/60",
            touched?.has(child.path) && "ide-touched",
          )}
        >
          <FileGlyph path={child.path} />
          <span
            className={cn(
              "truncate",
              status === "added" && "text-[var(--success)]",
              status !== "unchanged" && "font-medium",
            )}
            dir="ltr"
          >
            {child.name}
          </span>
          <span className="ms-auto flex items-center gap-2">
            <StatusLetter status={status} />
          </span>
        </button>
      );
    });
  return <div className="flex flex-col gap-px">{level(root, 0)}</div>;
}

// ---------- code and diff ----------

/** Highlighted code; ``mark`` is the changed span of the line, shown as a word highlight. */
function Code({
  path,
  text,
  mark,
  tone,
}: {
  path: string;
  text: string;
  mark?: [number, number];
  tone?: "add" | "del";
}) {
  const out: React.ReactNode[] = [];
  let at = 0;
  highlightLine(path, text).forEach((t, n) => {
    const end = at + t.text.length;
    // Split each token where the changed span starts and ends.
    const cuts = mark
      ? [at, Math.min(Math.max(mark[0], at), end), Math.min(Math.max(mark[1], at), end), end]
      : [at, end];
    for (let k = 0; k < cuts.length - 1; k++) {
      const from = cuts[k]!;
      const to = cuts[k + 1]!;
      if (to <= from) continue;
      const hot = mark && k === 1;
      out.push(
        <span
          key={`${n}-${k}`}
          className={cn(
            t.cls,
            hot && "rounded-[3px] [box-decoration-break:clone]",
            hot && (tone === "add" ? "bg-[var(--success)]/20" : "bg-[var(--danger)]/20"),
          )}
        >
          {text.slice(from, to)}
        </span>,
      );
    }
    at = end;
  });
  return <>{out}</>;
}

/** The changed middle of a replaced line, after the shared start and end. */
function changedSpan(a: string, b: string): { a: [number, number]; b: [number, number] } | null {
  let s = 0;
  while (s < a.length && s < b.length && a[s] === b[s]) s++;
  let e = 0;
  while (e < a.length - s && e < b.length - s && a[a.length - 1 - e] === b[b.length - 1 - e]) e++;
  // Mark only a real edit inside the line, not a whole new line.
  if (s + e === 0) return null;
  return { a: [s, a.length - e], b: [s, b.length - e] };
}

/** Word highlights for each removed line that a later added line replaces, pair by pair. */
function wordMarks(lines: DiffLine[]): Map<DiffLine, [number, number]> {
  const marks = new Map<DiffLine, [number, number]>();
  for (let i = 0; i < lines.length; ) {
    if (lines[i]!.kind !== "del") {
      i++;
      continue;
    }
    const dels: DiffLine[] = [];
    while (lines[i]?.kind === "del") dels.push(lines[i++]!);
    const adds: DiffLine[] = [];
    while (lines[i]?.kind === "add") adds.push(lines[i++]!);
    dels.forEach((d, k) => {
      const a = adds[k];
      const span = a && changedSpan(d.text, a.text);
      if (!a || !span) return;
      marks.set(d, span.a);
      marks.set(a, span.b);
    });
  }
  return marks;
}

/**
 * A reader's note on one line of a file's draft text, sent to the agent with
 * the next message. `id` is stable while the note lives.
 */
export type PaneComment = {
  id: string;
  path: string;
  line: number;
  text: string;
};

type CommentHandlers = {
  comments?: readonly PaneComment[];
  onComment?: (c: Omit<PaneComment, "id">) => void;
  onRemoveComment?: (id: string) => void;
  onEditComment?: (id: string, text: string) => void;
};

/** Where a diff line sits: deleted lines count in the old file, all others in the new one. */
function lineRef(line: DiffLine): { side: "old" | "new"; no: number } {
  return line.kind === "del"
    ? { side: "old", no: line.oldNo ?? 0 }
    : { side: "new", no: line.newNo ?? 0 };
}

// Changed gutters sit a step darker than their line, as in Codex.
const GUTTER_SHADE =
  "[box-shadow:inset_0_0_0_999px_rgb(0_0_0/0.05)] dark:[box-shadow:inset_0_0_0_999px_rgb(0_0_0/0.3)]";

/** One side of a diff row: a number column with a 3px bar, then the code. */
function Half({
  path,
  line,
  old = false,
  mark,
  wrap,
  onAdd,
}: {
  path: string;
  line?: DiffLine;
  /** The split view's left side, which numbers every line in the old file. */
  old?: boolean;
  mark?: [number, number];
  wrap: boolean;
  onAdd?: () => void;
}) {
  // Settings > Code > Diff markers: "+/−" swaps the row colors for a sign column.
  const signs = useCodeAppearance().markers === "signs";
  // An empty side in the split view: the other side added or removed a line.
  if (!line)
    return (
      <div className="min-w-0 flex-1 bg-[repeating-linear-gradient(135deg,transparent_0_5px,var(--border)_5px_6px)] opacity-60" />
    );
  const kind = line.kind;
  const ref = lineRef(line);
  const no = old ? (line.oldNo ?? 0) : ref.no;
  const tint = !signs && kind !== "same";
  return (
    <div
      className={cn(
        "group/line flex min-w-0 flex-1",
        tint && kind === "add" && "bg-[var(--success-dim)]",
        tint && kind === "del" && "bg-[var(--danger-dim)]",
        !tint && "[@media(hover:hover)]:hover:bg-foreground/[0.03]",
      )}
    >
      <span
        className={cn(
          "relative w-12 shrink-0 select-none border-s-[3px] pe-3 text-end tabular-nums",
          tint && kind === "add" && cn("border-[var(--success)] text-[var(--success)]", GUTTER_SHADE),
          tint && kind === "del" && cn("border-[var(--danger)] text-[var(--danger)]", GUTTER_SHADE),
          !tint && "border-transparent text-muted-foreground",
        )}
      >
        {no}
        {onAdd && (
          <button
            type="button"
            onClick={onAdd}
            aria-label={formatMsg("submit.agentic_ide.review.comment_on_line", { line: ref.no })}
            className="absolute inset-y-0 start-1 my-auto flex size-[1.125rem] items-center justify-center rounded-[4px] bg-foreground text-background opacity-0 transition-opacity duration-150 hover:bg-foreground/85 focus-visible:opacity-100 group-hover/line:opacity-100 [@media(hover:none)]:opacity-50"
          >
            <Plus className="size-3" weight="bold" aria-hidden />
          </button>
        )}
      </span>
      {signs && (
        <span
          aria-hidden
          className={cn(
            "w-4 shrink-0 select-none text-center",
            kind === "add" && "text-[var(--success)]",
            kind === "del" && "text-[var(--danger)]",
          )}
        >
          {kind === "add" ? "+" : kind === "del" ? "−" : ""}
        </span>
      )}
      <span
        className={cn(
          signs ? "min-w-0 flex-1 ps-1 pe-6" : "min-w-0 flex-1 ps-3 pe-6",
          wrap ? "whitespace-pre-wrap break-all" : "whitespace-pre",
        )}
      >
        <Code path={path} text={line.text} mark={mark} tone={kind === "add" ? "add" : "del"} />
      </span>
    </div>
  );
}

/** Pair removed and added lines side by side, as a split diff shows them. */
function pairLines(lines: DiffLine[]): Array<{ left?: DiffLine; right?: DiffLine }> {
  const rows: Array<{ left?: DiffLine; right?: DiffLine }> = [];
  for (let i = 0; i < lines.length; ) {
    const line = lines[i]!;
    if (line.kind === "same") {
      rows.push({ left: line, right: line });
      i++;
      continue;
    }
    const dels: DiffLine[] = [];
    while (lines[i]?.kind === "del") dels.push(lines[i++]!);
    const adds: DiffLine[] = [];
    while (lines[i]?.kind === "add") adds.push(lines[i++]!);
    for (let k = 0; k < Math.max(dels.length, adds.length); k++)
      rows.push({ left: dels[k], right: adds[k] });
  }
  return rows;
}

const IMPORT_LINE = /^\s*(import\s|from\s+\S+\s+import\s|export\s.*\sfrom\s)/;

// Codex opens a fold 20 lines per click.
const FOLD_STEP = 20;

/** The reader's diff settings, from the Changes options menu. */
export type DiffOptions = {
  mode?: "unified" | "split";
  wrap?: boolean;
  wordDiffs?: boolean;
  ignoreSpace?: boolean;
  hideImports?: boolean;
};

/** A file diff, Codex style: unified or split, and folded unchanged lines open 20 at a time. */
export function DiffView({
  file,
  context = 3,
  mode = "unified",
  wrap = false,
  wordDiffs = true,
  ignoreSpace = false,
  hideImports = false,
  foldable = true,
  comments,
  onComment,
  onRemoveComment,
  onEditComment,
}: {
  file: RepoFile;
  context?: number;
  /** Off, a fold only counts the hidden lines and does not open. */
  foldable?: boolean;
} & DiffOptions &
  CommentHandlers) {
  const lines = React.useMemo(() => {
    const all = diffLines(file.before, file.after, ignoreSpace);
    // "Hide imports" drops changed import lines; unchanged ones stay as context.
    // A changed import line with a comment stays, so its comment does not hide.
    const commented = (l: DiffLine) =>
      l.kind !== "del" && !!comments?.some((c) => c.path === file.path && c.line === l.newNo);
    return hideImports
      ? all.filter((l) => l.kind === "same" || !IMPORT_LINE.test(l.text) || commented(l))
      : all;
  }, [file, ignoreSpace, hideImports, comments]);
  const blocks = React.useMemo(() => diffBlocks(lines, context), [lines, context]);
  const marks = React.useMemo(
    () => (wordDiffs ? wordMarks(lines) : new Map<DiffLine, [number, number]>()),
    [lines, wordDiffs],
  );
  const [shown, setShown] = React.useState<Map<number, number>>(() => new Map());
  const [drafting, setDrafting] = React.useState<string | null>(null);
  const idOf = (line: DiffLine) => {
    const ref = lineRef(line);
    return `${ref.side}:${ref.no}`;
  };
  // Comments live on the draft's lines only: a removed line has no place to stay.
  const draftText = React.useRef("");
  const addFor = (line?: DiffLine) =>
    line && line.kind !== "del" && onComment
      ? () => {
          draftText.current = line.text;
          setDrafting(idOf(line));
        }
      : undefined;
  // A new text moves line numbers: unfolded gaps reset, and an open box closes unless its line stays.
  const shownText = React.useRef(file.after);
  React.useEffect(() => {
    if (shownText.current === file.after) return;
    shownText.current = file.after;
    setShown(new Map());
    setDrafting((d) =>
      d && lines.some((l) => l.kind !== "del" && idOf(l) === d && l.text === draftText.current) ? d : null,
    );
    // Only a new text resets; `lines` is read for the check, not watched.
  }, [file.after]);
  // Other options give other blocks, so the stored fold counts no longer fit.
  const shownOptions = React.useRef(`${ignoreSpace}|${hideImports}|${context}`);
  React.useEffect(() => {
    const key = `${ignoreSpace}|${hideImports}|${context}`;
    if (shownOptions.current === key) return;
    shownOptions.current = key;
    setShown(new Map());
  }, [ignoreSpace, hideImports, context]);
  const notes = (line?: DiffLine, layout?: "split") => {
    if (!line) return null;
    const ref = lineRef(line);
    const inner = notesFor(line, ref);
    if (!inner || layout !== "split") return inner;
    // In the split view a note sits under its own side: old on the left, new on the right.
    return (
      <div className="grid grid-cols-[minmax(0,1fr)_1px_minmax(0,1fr)]">
        <div className={ref.side === "old" ? "col-start-1" : "col-start-3"}>{inner}</div>
        <span className="col-start-2 row-start-1 bg-border/70" />
      </div>
    );
  };
  const notesFor = (line: DiffLine, ref: { side: "old" | "new"; no: number }) => {
    const here =
      (ref.side === "new" && comments?.filter((c) => c.path === file.path && c.line === ref.no)) ||
      [];
    if (!here.length && !(drafting === idOf(line) && onComment)) return null;
    return (
      <>
        {here.map((c) => (
          <CommentCard
            key={c.id}
            comment={c}
            onRemove={onRemoveComment && (() => onRemoveComment(c.id))}
            onEdit={onEditComment && ((text) => onEditComment(c.id, text))}
          />
        ))}
        {drafting === idOf(line) && onComment && (
          <CommentDraft
            onCancel={() => setDrafting(null)}
            onSave={(text) => {
              onComment({ path: file.path, line: ref.no, text });
              setDrafting(null);
            }}
          />
        )}
      </>
    );
  };
  const renderLines = (part: DiffLine[], key: string) =>
    mode === "split"
      ? pairLines(part).map(({ left, right }, k) => (
          <React.Fragment key={`${key}-${k}`}>
            <div className={cn("flex", !wrap && "min-w-max")}>
              {/* Comments sit on the draft's lines, so only the right side takes them. */}
              <Half path={file.path} line={left} old mark={left && marks.get(left)} wrap={wrap} />
              <span className="w-px shrink-0 bg-border/70" />
              <Half
                path={file.path}
                line={right}
                mark={right && marks.get(right)}
                wrap={wrap}
                onAdd={addFor(right)}
              />
            </div>
            {notes(left, "split")}
            {right !== left && notes(right, "split")}
          </React.Fragment>
        ))
      : part.map((line, k) => (
          <React.Fragment key={`${key}-${k}`}>
            <div className={cn("flex", !wrap && "min-w-max")}>
              <Half
                path={file.path}
                line={line}
                mark={marks.get(line)}
                wrap={wrap}
                onAdd={addFor(line)}
              />
            </div>
            {notes(line)}
          </React.Fragment>
        ));
  if (!lines.some((l) => l.kind !== "same"))
    return (
      <p className="px-4 py-3 font-sans text-[12.5px] text-muted-foreground">
        {msg("submit.agentic_ide.review.no_changes_options")}
      </p>
    );
  return (
    <div className="ide-code font-mono text-[12.5px] leading-[1.7]" dir="ltr">
      {blocks.map((block, n) => {
        if (block.kind === "lines") return renderLines(block.lines, `${n}`);
        // The lines nearest the change open first, so the fold shrinks from that side.
        // A fold with a comment in it stays open, so no comment hides.
        const pinned = block.lines.some(
          (l) => l.kind !== "del" && comments?.some((c) => c.path === file.path && c.line === l.newNo),
        );
        const open = pinned ? block.lines.length : Math.min(shown.get(n) ?? 0, block.lines.length);
        const hidden = block.lines.length - open;
        const fromTop = n > 0;
        const visible = fromTop ? block.lines.slice(0, open) : block.lines.slice(hidden);
        const bar =
          hidden > 0 ? (
            <button
              key={`fold-${n}`}
              type="button"
              disabled={!foldable}
              onClick={() => setShown((prev) => new Map(prev).set(n, open + FOLD_STEP))}
              className="flex h-7 w-full items-center bg-muted/80 px-4 text-start font-sans text-[12px] text-muted-foreground transition-colors duration-150 enabled:hover:bg-muted enabled:hover:text-foreground disabled:cursor-default"
            >
              {formatMsg("submit.agentic_ide.review.unmodified_lines", { count: hidden })}
            </button>
          ) : null;
        const rows = renderLines(visible, `${n}-f`);
        return (
          <React.Fragment key={n}>
            {fromTop ? rows : bar}
            {fromTop ? bar : rows}
          </React.Fragment>
        );
      })}
    </div>
  );
}

// A comment box starts where the code starts and stays in view when a long
// line scrolls the diff sideways.
const COMMENT_BOX = "sticky start-0 ms-12 my-1.5 w-[min(36rem,calc(100%-3rem-12px))] font-sans";

/** The comment author: the signed-in account's name and picture. */
function useCommentAuthor(): { name: string; image: string | null } {
  const session = useSession().data?.user;
  // The Mac app's session user is a local placeholder, so its name comes from the linked account, as in the account menu.
  const linked = useLinkedAccount();
  const user = linked?.linked ? linked : session;
  return {
    name: user?.name || user?.email || msg("submit.agentic_ide.review.you"),
    image: user?.image ?? null,
  };
}

/** The author's picture, or the first letter of the name when there is none. */
function AuthorAvatar({ className }: { className?: string }) {
  const { name, image } = useCommentAuthor();
  return image ? (
    // Avatars come from any OAuth host, so a plain <img> skips next/image host config, as the account menu does.
    <img
      src={image}
      alt=""
      referrerPolicy="no-referrer"
      className={cn("size-5 shrink-0 rounded-full object-cover", className)}
    />
  ) : (
    <span
      aria-hidden
      className={cn(
        "grid size-5 shrink-0 place-items-center rounded-full bg-primary-fill text-[10px] font-semibold uppercase text-primary-foreground",
        className,
      )}
    >
      {name.slice(0, 1)}
    </span>
  );
}

/** Who wrote a comment: the signed-in account's picture and name. */
function CommentHead({ children }: { children?: React.ReactNode }) {
  const who = useCommentAuthor().name;
  return (
    <div className="flex items-center gap-2">
      <AuthorAvatar />
      <span dir="auto" className="min-w-0 truncate text-[12.5px] font-medium text-foreground">
        {who}
      </span>
      <span className="ms-auto flex items-center">{children}</span>
    </div>
  );
}

function KeyHint({ label, keys }: { label: string; keys: string }) {
  return (
    <span className="flex items-center gap-2">
      {label}
      <span className="text-muted-foreground">{keys}</span>
    </span>
  );
}

/** The open box for a note on one line. "Add comment" (⌘↵) queues it for the agent's next message; Esc closes it. */
function CommentDraft({
  onSave,
  onCancel,
  initial = "",
}: {
  onSave: (text: string) => void;
  onCancel: () => void;
  /** An edit starts from the saved text. */
  initial?: string;
}) {
  const [text, setText] = React.useState(initial);
  const editing = initial !== "";
  const canSave = !!text.trim() && text.trim() !== initial;
  const save = () => canSave && onSave(text.trim());
  const saveLabel = msg(
    editing ? "submit.agentic_ide.review.save_comment" : "submit.agentic_ide.review.add_comment",
  );
  return (
    // The row opens to make room while the box fades and settles in. Clip, not hidden, keeps the box sticky.
    <div className="ide-comment-row">
      <div className="min-h-0 overflow-clip [overflow-clip-margin:12px]">
        <div
          className={cn(
            COMMENT_BOX,
            "ide-comment-in ide-line rounded-xl border border-border bg-card p-3 shadow-[0_4px_16px_-8px_rgba(19,19,19,0.18)]",
          )}
        >
          <CommentHead />
          <textarea
            autoFocus
            dir="auto"
            rows={2}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                save();
              }
              if (e.key === "Escape") onCancel();
            }}
            placeholder={msg("submit.agentic_ide.review.comment_placeholder")}
            className="mt-2 block w-full resize-none bg-transparent text-[13px] leading-5 text-foreground outline-none placeholder:text-muted-foreground"
          />
          <div className="mt-2 grid grid-cols-2 gap-2">
            <TooltipButton
              delayDuration={250}
              tooltip={<KeyHint label={msg("submit.agentic_ide.review.cancel")} keys="Esc" />}
            >
              <button
                type="button"
                aria-label={msg("submit.agentic_ide.review.cancel")}
                onClick={onCancel}
                className="flex h-7 items-center justify-center rounded-md border border-border text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                <X className="size-3.5" />
              </button>
            </TooltipButton>
            <TooltipButton
              delayDuration={250}
              tooltip={<KeyHint label={saveLabel} keys={`${commandKey()}↵`} />}
            >
              <button
                type="button"
                aria-label={saveLabel}
                onClick={save}
                disabled={!canSave}
                className="flex h-7 items-center justify-center rounded-md bg-primary-fill text-primary-foreground transition-[opacity,filter] hover:brightness-110 disabled:opacity-35"
              >
                <Check className="size-3.5" />
              </button>
            </TooltipButton>
          </div>
        </div>
      </div>
    </div>
  );
}

/** A queued comment's icon action, with a tooltip. */
function CommentAction({
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
          "grid size-6 place-items-center rounded-md text-muted-foreground transition-colors duration-150 hover:bg-accent",
          danger ? "hover:text-[var(--danger)]" : "hover:text-foreground",
        )}
      >
        {children}
      </button>
    </TooltipButton>
  );
}

/** A saved comment under its line; the agent gets it with the next message. */
function CommentCard({
  comment,
  onRemove,
  onEdit,
}: {
  comment: PaneComment;
  onRemove?: () => void;
  onEdit?: (text: string) => void;
}) {
  const [editing, setEditing] = React.useState(false);
  if (editing && onEdit)
    return (
      <CommentDraft
        initial={comment.text}
        onCancel={() => setEditing(false)}
        onSave={(text) => {
          onEdit(text);
          setEditing(false);
        }}
      />
    );
  return (
    <div
      data-comment-id={comment.id}
      className={cn(COMMENT_BOX, "group/comment scroll-my-6 rounded-xl border border-border bg-card px-3 py-2.5")}
    >
      <CommentHead>
        <span className="-my-1 flex items-center gap-0.5 opacity-0 transition-opacity duration-150 focus-within:opacity-100 group-hover/comment:opacity-100 [@media(hover:none)]:opacity-100">
          {onEdit && (
            <CommentAction
              label={msg("submit.agentic_ide.review.edit_comment")}
              onClick={() => setEditing(true)}
            >
              <PencilSimple className="size-3.5" aria-hidden />
            </CommentAction>
          )}
          {onRemove && (
            <CommentAction
              label={msg("submit.agentic_ide.review.delete_comment")}
              onClick={onRemove}
              danger
            >
              <Trash className="size-3.5" aria-hidden />
            </CommentAction>
          )}
        </span>
      </CommentHead>
      <p dir="auto" className="mt-1.5 whitespace-pre-wrap break-words text-[13px] leading-5 text-foreground">
        {comment.text}
      </p>
    </div>
  );
}

export function Spinner() {
  return <CircleNotch className="size-3.5 animate-spin" />;
}
