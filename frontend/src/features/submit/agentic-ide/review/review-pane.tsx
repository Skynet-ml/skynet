import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";
import { FileMagnifyingGlassIcon } from "@phosphor-icons/react/dist/ssr/FileMagnifyingGlass";
import { HighlighterIcon } from "@phosphor-icons/react/dist/ssr/Highlighter";
import { ParagraphIcon } from "@phosphor-icons/react/dist/ssr/Paragraph";
import { RowsIcon } from "@phosphor-icons/react/dist/ssr/Rows";

import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { MessageMarkdown } from "@/shared/ui/agent/message-markdown";
import {
  ArrowSquareOut,
  ArrowsInLineVertical,
  BracketsCurly,
  CaretDown,
  CaretLeft,
  CaretRight,
  CaretUp,
  ChatText,
  Columns,
  DotsThree,
  Eye,
  FileText,
  GitDiff,
  GithubLogo,
  Info,
  ListBullets,
  MagnifyingGlass,
  Plus,
  TextAlignLeft,
  X,
} from "@/shared/ui/icons";
import {
  DropdownMenu,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/primitives/popover";
import { commandKey, TooltipButton } from "@/shared/ui/tooltip-button";
import { Bitbucket, GitLab } from "@/shared/ui/brand-marks";

import { CodeAppearanceStyle } from "./code-style";
import { CodeEditor, type ChangeNav, type ChangeNavHandle } from "./editor";
import {
  baseName,
  buildTree,
  dirName,
  fuzzyScore,
  statusOf,
  type FileStatus,
  type RepoFile,
} from "./lib";
import { MENU_ROW, MiniSwitch, PaneLabel, PaneMenuContent, PaneSeparator } from "./menu-look";
import {
  Caret,
  Counts,
  DiffView,
  FileCounts,
  FileGlyph,
  FileTree,
  PathLabel,
  Spinner,
  StatusLetter,
  statusName,
  totals,
  type DiffOptions,
  type PaneComment,
} from "./parts";

// Codex's motion tokens: 150ms for small state changes, 250ms for the file list.
const LIST_EASE = [0.25, 1, 0.5, 1] as const;
// Ease-out quint: a file opens fast and settles softly, with no bounce.
const FOLD_EASE = [0.22, 1, 0.36, 1] as const;
const LIST_WIDTH = 248;
// Codex moves Word wrap, Collapse all and the diff mode inline from 625px.
const INLINE_TOOLS_WIDTH = 625;
// The jump list renders its best matches only; a large repository has thousands of files.
const JUMP_LIMIT = 200;
const NEXT_MODE = { unified: "split", split: "unified" } as const;
const modeLabel = (mode: "unified" | "split") =>
  mode === "unified"
    ? msg("submit.agentic_ide.review.unified_diff")
    : msg("submit.agentic_ide.review.split_diff");

const isNewTab = (tab: string) => tab.startsWith("new:");

/**
 * A request from the chat to show a file's diff; a new object shows it again.
 * `paths`, when given, are the files of one turn, marked in the tree.
 */
type PaneRequest = { path: string; paths?: readonly string[] };

/** The repository's page on its host, for the Open on host button. */
export type RepoHostLink = { provider: "github" | "gitlab" | "bitbucket"; url: string };

type Props = {
  /** The draft's files and the repository's other files. */
  files: RepoFile[];
  request: PaneRequest | null;
  /** The agent is on a turn: the editor does not take edits. */
  readOnly: boolean;
  /** Write a file's new text after an edit in its tab. */
  onEdit: (path: string, text: string) => void;
  /** A file tab opened: its repository text is needed. */
  onOpenFile: (path: string) => void;
  comments: readonly PaneComment[];
  /** Absent when no chat takes comments. */
  onComment?: (c: Omit<PaneComment, "id">) => void;
  onRemoveComment?: (id: string) => void;
  onEditComment?: (id: string, text: string) => void;
  onHide: () => void;
  host?: RepoHostLink | null;
};

const iconButton =
  "flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors duration-150 hover:bg-accent hover:text-foreground data-[state=open]:bg-accent data-[state=open]:text-foreground";

/**
 * An icon button with a tooltip, which can show a shortcut. `pressed` is a
 * toggle with a fixed label; `active` only marks a button whose label already
 * says its state.
 */
function ToolButton({
  label,
  keys,
  pressed,
  active,
  disabled,
  onClick,
  children,
}: {
  label: string;
  keys?: string;
  pressed?: boolean;
  active?: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <TooltipButton
      delayDuration={250}
      tooltip={
        <span className="flex items-center gap-2">
          {label}
          {keys && <span className="text-muted-foreground">{keys}</span>}
        </span>
      }
    >
      <button
        type="button"
        aria-label={label}
        aria-pressed={pressed}
        disabled={disabled}
        onClick={onClick}
        className={cn(
          iconButton,
          (pressed || active) && "bg-accent text-foreground",
          "disabled:pointer-events-none disabled:opacity-40",
        )}
      >
        {children}
      </button>
    </TooltipButton>
  );
}

const REPO_HOSTS = {
  github: { name: "GitHub", Mark: GithubLogo },
  gitlab: { name: "GitLab", Mark: GitLab.Mark },
  bitbucket: { name: "Bitbucket", Mark: Bitbucket.Mark },
};

/** Opens the repository on the host the setup step used, shown by that host's mark. */
function RepoHostButton({ host }: { host: RepoHostLink }) {
  const { name, Mark } = REPO_HOSTS[host.provider];
  const label = formatMsg("submit.agentic_ide.review.open_on_host", { host: name });
  return (
    <TooltipButton delayDuration={250} tooltip={label}>
      <a href={host.url} target="_blank" rel="noreferrer" aria-label={label} className={iconButton}>
        <Mark size={16} />
      </a>
    </TooltipButton>
  );
}

/** A menu trigger with a tooltip; Radix needs the trigger inside the tooltip. */
function MenuButton({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <TooltipButton delayDuration={250} tooltip={label}>
      <DropdownMenuTrigger asChild>
        <button type="button" aria-label={label} className={iconButton}>
          {children}
        </button>
      </DropdownMenuTrigger>
    </TooltipButton>
  );
}

/** A menu row with an icon at the start and, for a setting, a switch at the end. */
function MenuRow({
  icon,
  label,
  checked,
  keepOpen,
  onSelect,
}: {
  icon?: React.ReactNode;
  label: string;
  checked?: boolean;
  keepOpen?: boolean;
  onSelect: () => void;
}) {
  return (
    <DropdownMenuItem
      onSelect={(e) => {
        // Settings stay open, so the reader can flip several in a row.
        if (keepOpen) e.preventDefault();
        onSelect();
      }}
      role={checked === undefined ? "menuitem" : "menuitemcheckbox"}
      aria-checked={checked}
      className={MENU_ROW}
    >
      <span className="flex size-4 shrink-0 items-center justify-center text-muted-foreground [&>svg]:size-4">
        {icon}
      </span>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {checked !== undefined && <MiniSwitch on={checked} />}
    </DropdownMenuItem>
  );
}

/** What the diffs compare: the draft against the repository, with the line totals. */
function SourceLabel({ sum }: { sum: { added: number; removed: number } }) {
  return (
    <span className="flex h-7 items-center gap-1.5 rounded-md bg-muted px-3 text-[12.5px] font-medium text-foreground">
      {msg("submit.agentic_ide.review.source")}
      <Counts {...sum} className="font-sans text-[12px]" />
    </span>
  );
}

const STATUS_TEXT: Record<FileStatus, string> = {
  added: "text-[var(--success)]",
  modified: "text-[var(--warning)]",
  unchanged: "text-foreground/85",
};

/** Codex's square status badge: a dot for a change, a plus for a new file. */
function StatusBadge({ status }: { status: FileStatus }) {
  if (status === "unchanged") return null;
  return (
    <span
      title={statusName(status)}
      className={cn(
        "relative flex size-4 shrink-0 items-center justify-center rounded-[4px] border-[1.5px] border-current",
        STATUS_TEXT[status].split(" ")[0],
      )}
    >
      {status === "modified" && <span className="size-1.5 rounded-full bg-current" />}
      {status === "added" && (
        <>
          <span className="h-[1.5px] w-2 bg-current" />
          <span className="absolute h-2 w-[1.5px] bg-current" />
        </>
      )}
    </span>
  );
}

function Section({
  label,
  marker,
  children,
}: {
  label: string;
  marker?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(true);
  return (
    <div className="flex flex-col">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex h-7 items-center gap-1.5 rounded-md px-1.5 text-start text-[12.5px] font-medium text-foreground transition-colors hover:bg-accent/60"
      >
        <Caret open={open} className="text-muted-foreground" />
        {label}
        {/* Codex marks a group with unreviewed changes by a dot, not a count. */}
        {marker && (
          <span
            role="img"
            className="ms-auto me-1 size-1.5 rounded-full bg-primary"
            aria-label={msg("submit.agentic_ide.review.unreviewed")}
          />
        )}
      </button>
      {/* Codex shows and hides a section at once. */}
      {open && children}
    </div>
  );
}

/** A Markdown file as the reader will see it, for "Rich preview". */
function RichPreview({ file }: { file: RepoFile }) {
  return (
    <div className="border-s-[3px] border-[var(--success)] px-6 py-4 text-[13.5px]">
      <MessageMarkdown content={file.after} />
    </div>
  );
}

const LARGE_DIFF_FILES = 128;
const LARGE_DIFF_LINES = 9_000;

/** Codex's large diff notice: one file shows at a time, with Previous and Next to step through the rest. */
function LargeDiffBanner({
  index,
  count,
  onStep,
}: {
  index: number;
  count: number;
  onStep: (index: number) => void;
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex h-9 shrink-0 items-center gap-2 border-b border-border/70 bg-[color-mix(in_oklab,var(--primary)_8%,var(--card))] ps-3 pe-1.5 text-[12.5px]"
    >
      <Info className="size-3.5 shrink-0 text-primary" aria-hidden />
      <span className="min-w-0 truncate">{msg("submit.agentic_ide.review.large_diff")}</span>
      <span className="ms-auto flex shrink-0 items-center gap-0.5">
        <ToolButton
          label={msg("submit.agentic_ide.review.previous_file")}
          disabled={index <= 0}
          onClick={() => onStep(index - 1)}
        >
          <CaretLeft className="size-3.5 rtl:-scale-x-100" />
        </ToolButton>
        <span className="min-w-12 text-center tabular-nums text-muted-foreground">
          {formatMsg("submit.agentic_ide.review.file_position", { index: index + 1, count })}
        </span>
        <ToolButton
          label={msg("submit.agentic_ide.review.next_file")}
          disabled={index >= count - 1}
          onClick={() => onStep(index + 1)}
        >
          <CaretRight className="size-3.5 rtl:-scale-x-100" />
        </ToolButton>
      </span>
    </div>
  );
}

const isMarkdown = (path: string) => /\.mdx?$/i.test(path);

// Codex's "folder-on-folder" glyph for the files toggle: two stacked folders.
function FilesIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" fill="currentColor" aria-hidden className={className}>
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M7.60791 1.47461C8.06198 1.47464 8.50506 1.61631 8.87549 1.87891L9.53662 2.34766C9.72953 2.48444 9.96128 2.55852 10.1978 2.55859H12.9995C14.2099 2.55859 15.1919 3.53958 15.1919 4.75V9C15.1919 10.2104 14.2099 11.1914 12.9995 11.1914H11.8589V11.667C11.8587 12.8773 10.8768 13.8584 9.6665 13.8584H3.00049C1.79017 13.8584 0.808271 12.8773 0.808105 11.667V6.33398C0.808105 5.12356 1.79006 4.1416 3.00049 4.1416H4.14111V3.66699C4.14111 2.45669 5.12324 1.47481 6.3335 1.47461H7.60791ZM3.00049 5.19238C2.36996 5.19238 1.85889 5.70346 1.85889 6.33398V11.667C1.85905 12.2974 2.37006 12.8086 3.00049 12.8086H9.6665C10.2969 12.8086 10.8079 12.2974 10.8081 11.667V7.41699C10.8081 6.78647 10.297 6.27539 9.6665 6.27539H6.86475C6.41064 6.27539 5.96763 6.1337 5.59717 5.87109L4.93506 5.40234C4.7421 5.26554 4.51144 5.19241 4.2749 5.19238H3.00049ZM6.3335 2.52539C5.70314 2.52559 5.19189 3.03659 5.19189 3.66699V4.34375C5.31402 4.4001 5.43168 4.46733 5.54248 4.5459L6.20361 5.01465C6.39659 5.15144 6.6282 5.22559 6.86475 5.22559H9.6665C10.8769 5.22559 11.8589 6.20657 11.8589 7.41699V10.1416H12.9995C13.63 10.1416 14.1411 9.63052 14.1411 9V4.75C14.1411 4.11947 13.63 3.6084 12.9995 3.6084H10.1978C9.74372 3.60832 9.30057 3.46671 8.93018 3.2041L8.26807 2.73535C8.07516 2.59866 7.84434 2.52542 7.60791 2.52539H6.3335Z"
      />
    </svg>
  );
}

/** A file the repository cannot give as text, or one still on its way. */
function FileNotice({ file }: { file: RepoFile }) {
  if (file.loading)
    return (
      <p className="flex items-center gap-2 p-6 text-[13px] text-muted-foreground" role="status">
        <Spinner />
        {msg("submit.agentic_ide.loading_file")}
      </p>
    );
  if (!file.issue) return null;
  return (
    <p className="p-6 text-[13px] text-muted-foreground">
      {msg(`submit.agentic_ide.review.issue.${file.issue}`)}
    </p>
  );
}

/**
 * The Changes pane: every changed file's diff, a tab per opened file with the
 * editor, and the file list with the whole repository tree.
 */
export function ReviewPane({
  files,
  request,
  readOnly,
  onEdit,
  onOpenFile,
  comments,
  onComment,
  onRemoveComment,
  onEditComment,
  onHide,
  host,
}: Props) {
  const changed = React.useMemo(() => files.filter((f) => statusOf(f) !== "unchanged"), [files]);
  const tree = React.useMemo(() => buildTree(files), [files]);
  const byPath = React.useMemo(() => new Map(files.map((f) => [f.path, f])), [files]);
  const sum = totals(changed);
  // Codex's large diff mode: past 128 files or 9,000 changed lines it shows one file at a time.
  const large = changed.length > LARGE_DIFF_FILES || sum.added + sum.removed > LARGE_DIFF_LINES;

  // A tab is a file path, or "new:<n>" for a New tab page.
  const [tabs, setTabs] = React.useState<string[]>([]);
  const [active, setActive] = React.useState<"changes" | string>("changes");
  // Closing Changes keeps the pane while file tabs stay open; any jump to a diff reopens it.
  const [changesOpen, setChangesOpen] = React.useState(true);
  React.useEffect(() => {
    if (active === "changes") setChangesOpen(true);
  }, [active]);
  // Codex keeps one preview tab (italic title); an edit or a double click pins it.
  const [preview, setPreview] = React.useState<string | null>(null);
  const [showList, setShowList] = React.useState(true);
  const [collapsed, setCollapsed] = React.useState<Set<string>>(() => new Set());
  const [filter, setFilter] = React.useState("");
  const [current, setCurrent] = React.useState<string | null>(null);
  const [flash, setFlash] = React.useState<{ path: string; n: number } | null>(null);
  const [touched, setTouched] = React.useState<ReadonlySet<string>>(() => new Set());
  const [hideChanges, setHideChanges] = React.useState<Set<string>>(() => new Set());
  const [nav, setNav] = React.useState<ChangeNav>({ pos: 0, count: 0 });
  const navRef = React.useRef<ChangeNavHandle>(null);
  const [jump, setJump] = React.useState(false);
  const [jumpQuery, setJumpQuery] = React.useState("");

  // The Changes options, with Codex's defaults.
  const [wrap, setWrap] = React.useState(true);
  const [diffMode, setDiffMode] = React.useState<"unified" | "split">("unified");
  const [fullFiles, setFullFiles] = React.useState(true);
  const [rich, setRich] = React.useState(false);
  const [wordDiffs, setWordDiffs] = React.useState(true);
  const [ignoreSpace, setIgnoreSpace] = React.useState(false);
  const [hideImports, setHideImports] = React.useState(false);

  const root = React.useRef<HTMLDivElement>(null);
  const tabIds = React.useId();
  const panelId = `${tabIds}-panel`;
  const tabId = (tab: string) =>
    `${tabIds}-tab-${tab === "changes" ? "changes" : tabs.indexOf(tab)}`;
  const scroller = React.useRef<HTMLDivElement>(null);
  const sections = React.useRef(new Map<string, HTMLElement>());
  const [width, setWidth] = React.useState(0);
  React.useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => entry && setWidth(entry.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const nextMode = NEXT_MODE[diffMode];
  const ModeIcon = nextMode === "split" ? Columns : RowsIcon;
  const inlineTools = width + (showList ? LIST_WIDTH : 0) >= INLINE_TOOLS_WIDTH;
  const options: DiffOptions = { mode: diffMode, wrap, wordDiffs, ignoreSpace, hideImports };

  const openTab = (path: string, asPreview = false) => {
    onOpenFile(path);
    if (tabs.includes(path)) {
      if (!asPreview && preview === path) setPreview(null);
    } else {
      // A New tab page becomes the file it opens; otherwise a new preview replaces the old one.
      const replace = isNewTab(active)
        ? active
        : asPreview && preview && tabs.includes(preview)
          ? preview
          : null;
      setTabs(replace ? tabs.map((t) => (t === replace ? path : t)) : [...tabs, path]);
      if (asPreview) setPreview(path);
      else if (replace === preview) setPreview(null);
    }
    setActive(path);
  };
  const newTabs = React.useRef(0);
  const newTab = () => {
    const key = `new:${++newTabs.current}`;
    setTabs((t) => [...t, key]);
    setActive(key);
  };
  // Keep only `keep`; a closed active tab hands focus to its neighbor, then to Changes.
  // With Changes closed and no tab left, the pane hides.
  const keepTabs = (keep: string[]) => {
    if (!changesOpen && !keep.length) {
      setTabs([]);
      setActive("changes");
      onHide();
      return;
    }
    if (preview && !keep.includes(preview)) setPreview(null);
    if (active !== "changes" && !keep.includes(active)) {
      const i = tabs.indexOf(active);
      const after = tabs.slice(i + 1).find((t) => keep.includes(t));
      const before = tabs
        .slice(0, i)
        .reverse()
        .find((t) => keep.includes(t));
      setActive(after ?? before ?? keep[0] ?? "changes");
    }
    setTabs(keep);
  };
  const closeChanges = () => {
    if (!tabs.length) return onHide();
    setChangesOpen(false);
    if (active === "changes") setActive(tabs[0]!);
  };
  const closeTab = (tab: string) => keepTabs(tabs.filter((t) => t !== tab));
  // A tab whose file left the draft (a deleted new file, a reverted change) closes.
  const liveTabs = tabs.filter((t) => isNewTab(t) || byPath.has(t));
  React.useEffect(() => {
    if (liveTabs.length !== tabs.length) keepTabs(liveTabs);
  });
  const showDiff = (path: string) => {
    setActive("changes");
    setCurrent(path);
    setCollapsed((prev) => {
      const next = new Set(prev);
      next.delete(path);
      return next;
    });
    setFlash((f) => ({ path, n: (f?.n ?? 0) + 1 }));
    // Wait one frame so the Changes tab is mounted before the scroll.
    requestAnimationFrame(() => {
      const el = sections.current.get(path);
      const box = scroller.current;
      if (el && box) box.scrollTo({ top: el.offsetTop, behavior: "auto" });
    });
  };
  const go = (path: string, as: "diff" | "tab", asPreview = false) => {
    const file = byPath.get(path);
    if (as === "tab" || !file || statusOf(file) === "unchanged") openTab(path, asPreview);
    else showDiff(path);
  };

  React.useEffect(() => {
    if (!request) return;
    setTouched(new Set(request.paths ?? []));
    go(request.path, "diff");
    // Only a new request moves the pane.
  }, [request]);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Text fields outside the pane (the composer) keep the keys.
      const t = e.target;
      const typing =
        t instanceof HTMLElement &&
        (t.isContentEditable || t.tagName === "INPUT" || t.tagName === "TEXTAREA") &&
        !root.current?.contains(t);
      // A closed pane stays mounted; its shortcut must not act unseen.
      const unseen = root.current?.checkVisibility?.({ opacityProperty: true }) === false;
      if (!typing && !unseen && (e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "e") {
        e.preventDefault();
        setShowList((s) => !s);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  const allCollapsed = changed.length > 0 && changed.every((f) => collapsed.has(f.path));
  const collapseAll = () =>
    setCollapsed(allCollapsed ? new Set() : new Set(changed.map((f) => f.path)));
  const collapseLabel = allCollapsed
    ? msg("submit.agentic_ide.review.expand_all")
    : msg("submit.agentic_ide.review.collapse_all");
  const commentCount = (path: string) => comments.filter((c) => c.path === path).length;
  const matching = filter.trim() ? changed.filter((f) => fuzzyScore(filter, f.path) > 0) : changed;
  const newPage = isNewTab(active);
  const tabFile = active === "changes" || newPage ? null : (byPath.get(active) ?? null);
  const plainFile = !!tabFile && statusOf(tabFile) === "unchanged";
  // The jump list holds every repository file; the name scores first, then the full path.
  // It sorts only while open: the files change on every keystroke in the editor.
  const jumpMatches = React.useMemo(() => {
    if (!jump) return [];
    return (
      jumpQuery.trim()
        ? files
            .map((f) => ({
              f,
              score: fuzzyScore(jumpQuery, baseName(f.path)) || fuzzyScore(jumpQuery, f.path),
            }))
            .filter((m) => m.score > 0)
            .sort((x, y) => y.score - x.score)
            .map((m) => m.f)
        : [...files].sort(
            (x, y) =>
              baseName(x.path).localeCompare(baseName(y.path)) ||
              dirName(x.path).localeCompare(dirName(y.path)),
          )
    ).slice(0, JUMP_LIMIT);
  }, [jump, files, jumpQuery]);

  const listToggle = (
    <ToolButton
      label={
        showList
          ? msg("submit.agentic_ide.review.hide_files")
          : msg("submit.agentic_ide.review.show_files")
      }
      keys={`${commandKey()}⇧E`}
      active={showList}
      onClick={() => setShowList((s) => !s)}
    >
      <span className="relative">
        <FilesIcon className={cn("size-4", !showList && "opacity-60")} />
        {/* Codex dots the toggle while a file filter is on. */}
        {filter && <span className="absolute -end-0.5 -top-0.5 size-1.5 rounded-full bg-primary" />}
      </span>
    </ToolButton>
  );

  const jumpLabel = msg("submit.agentic_ide.review.jump_to_file");
  const jumpTo = (path: string) => {
    go(path, tabFile || newPage ? "tab" : "diff", true);
    setJump(false);
  };
  const jumpButton = (
    <Popover
      open={jump}
      onOpenChange={(o) => {
        setJump(o);
        if (o) setJumpQuery("");
      }}
    >
      <TooltipButton delayDuration={250} tooltip={jumpLabel}>
        <PopoverTrigger asChild>
          <button type="button" aria-label={jumpLabel} className={iconButton}>
            <FileMagnifyingGlassIcon className="size-4" />
          </button>
        </PopoverTrigger>
      </TooltipButton>
      <PopoverContent align="end" className="w-80 p-1.5">
        <label className="flex h-8 items-center gap-2 rounded-lg px-2 text-muted-foreground">
          <MagnifyingGlass className="size-3.5 shrink-0" aria-hidden />
          <input
            autoFocus
            value={jumpQuery}
            onChange={(e) => setJumpQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && jumpMatches[0]) jumpTo(jumpMatches[0].path);
            }}
            placeholder={msg("submit.agentic_ide.review.jump_placeholder")}
            aria-label={jumpLabel}
            className="min-w-0 flex-1 bg-transparent text-[13px] text-foreground outline-none placeholder:text-muted-foreground"
          />
        </label>
        <div className="my-1 h-px bg-border" />
        <div className="ide-scroll max-h-72 overflow-y-auto">
          {jumpMatches.map((f) => (
            <button
              key={f.path}
              type="button"
              onClick={() => jumpTo(f.path)}
              className="flex h-8 w-full items-center gap-2 rounded-lg px-2 text-start text-[13px] transition-colors duration-100 hover:bg-accent"
            >
              <FileGlyph path={f.path} />
              <PathLabel path={f.path} className="text-[12.5px]" />
              <span className="ms-auto flex items-center gap-2">
                {statusOf(f) !== "unchanged" && <FileCounts file={f} />}
                <StatusLetter status={statusOf(f)} />
              </span>
            </button>
          ))}
          {!jumpMatches.length && (
            <p className="px-2 py-2 text-[12.5px] text-muted-foreground">
              {msg("submit.agentic_ide.review.no_matching")}
            </p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );

  const changedRows = matching.map((f) => {
    const status = statusOf(f);
    const here = tabFile ? tabFile.path === f.path : current === f.path;
    const notes = commentCount(f.path);
    return (
      <button
        key={f.path}
        type="button"
        title={f.path}
        onClick={() => (tabFile || newPage ? openTab(f.path, true) : showDiff(f.path))}
        aria-current={here ? "true" : undefined}
        className={cn(
          "relative flex h-7 w-full shrink-0 items-center gap-1.5 rounded-md ps-5 pe-1.5 text-start text-[13px] transition-colors duration-150",
          here ? "bg-accent" : "hover:bg-accent/60",
          touched.has(f.path) && "font-semibold",
        )}
      >
        <FileGlyph path={f.path} />
        <span className={cn("min-w-0 flex-1 truncate", STATUS_TEXT[status])} dir="ltr">
          {baseName(f.path)}
        </span>
        {notes > 0 && (
          <span className="flex items-center gap-0.5 text-[11px] tabular-nums text-primary">
            <ChatText className="size-3" aria-hidden />
            {notes}
          </span>
        )}
        <FileCounts file={f} />
        <StatusBadge status={status} />
      </button>
    );
  });

  const body = (f: RepoFile) => {
    if (f.loading || f.issue) return <FileNotice file={f} />;
    if (rich && isMarkdown(f.path) && f.after !== "" && statusOf(f) === "added")
      return <RichPreview file={f} />;
    return (
      <DiffView
        key={f.path}
        file={f}
        context={3}
        // "Load full files" lets every fold open; off, the diff keeps only the hunks.
        foldable={fullFiles}
        {...options}
        comments={comments}
        onComment={onComment}
        onRemoveComment={onRemoveComment}
        onEditComment={onEditComment}
      />
    );
  };

  const tabView = (f: RepoFile) => {
    if (f.loading || f.issue) return <FileNotice file={f} />;
    if (rich && isMarkdown(f.path) && f.after !== "") return <RichPreview file={f} />;
    // A file tab is the editor, as in Codex; the draft's changes show inline until hidden.
    return (
      <CodeEditor
        key={f.path}
        path={f.path}
        base={f.before}
        value={f.after}
        readOnly={readOnly}
        onChange={(text) => {
          if (preview === f.path) setPreview(null);
          onEdit(f.path, text);
        }}
        review={statusOf(f) !== "unchanged" && !hideChanges.has(f.path)}
        wrap={wrap}
        onNav={setNav}
        navRef={navRef}
      />
    );
  };

  // In large mode the shown file is the current one while it still changes, else the first.
  const largePath = changed.some((f) => f.path === current) ? current : changed[0]?.path;
  const shownChanges = large ? changed.filter((f) => f.path === largePath) : changed;

  return (
    <div ref={root} className="flex h-full min-w-0 flex-1 flex-col bg-card">
      <CodeAppearanceStyle />
      {/* Tabs: Changes first, then each opened file. */}
      <div className="flex h-10 shrink-0 items-center gap-1 border-b border-border/70 bg-[var(--surface-2)] px-1.5">
        <div className="ide-scroll flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          <div className="flex shrink-0 items-center gap-1" role="tablist">
            {changesOpen && (
              <Tab
                id={tabId("changes")}
                panelId={panelId}
                selected={active === "changes"}
                onSelect={() => setActive("changes")}
                onClose={closeChanges}
                icon={<GitDiff className="size-3.5" />}
                label={msg("submit.agentic_ide.changes_tab")}
              />
            )}
            <AnimatePresence initial={false}>
              {tabs.map((tab) => (
                <motion.div
                  key={tab}
                  initial={{ opacity: 0, width: 0 }}
                  animate={{ opacity: 1, width: "auto" }}
                  exit={{ opacity: 0, width: 0 }}
                  transition={{ duration: 0.15, ease: "easeOut" }}
                  className="shrink-0 overflow-hidden"
                >
                  <Tab
                    id={tabId(tab)}
                    panelId={panelId}
                    selected={active === tab}
                    onSelect={() => setActive(tab)}
                    onPin={() => preview === tab && setPreview(null)}
                    onClose={() => closeTab(tab)}
                    icon={
                      isNewTab(tab) ? (
                        <Plus className="size-3.5" />
                      ) : (
                        <FileGlyph path={tab} className="size-3.5" />
                      )
                    }
                    label={isNewTab(tab) ? msg("submit.agentic_ide.review.new_tab") : baseName(tab)}
                    hint={isNewTab(tab) ? undefined : tab}
                    italic={preview === tab}
                  />
                </motion.div>
              ))}
            </AnimatePresence>
          </div>
          {/* Codex puts "+" right after the last tab, not at the end of the bar. */}
          <ToolButton label={msg("submit.agentic_ide.review.new_tab")} onClick={newTab}>
            <Plus className="size-4" />
          </ToolButton>
        </div>
      </div>

      {/* Toolbar for the active tab. */}
      <div className="flex h-11 shrink-0 items-center gap-2 border-b border-border/70 px-2">
        {newPage ? (
          <span className="ms-auto flex items-center gap-0.5">
            {jumpButton}
            {listToggle}
          </span>
        ) : tabFile ? (
          <>
            {statusOf(tabFile) !== "unchanged" && <SourceLabel sum={totals([tabFile])} />}
            <span className="ms-auto flex items-center gap-0.5">
              {statusOf(tabFile) !== "unchanged" &&
                !hideChanges.has(tabFile.path) &&
                !(rich && isMarkdown(tabFile.path)) && (
                  // Codex's change stepper: "n of count" once the cursor is in a change, the total before that.
                  <span className="me-1 flex items-center">
                    <ToolButton
                      label={msg("submit.agentic_ide.review.previous_change")}
                      disabled={!nav.count}
                      onClick={() => navRef.current?.step(-1)}
                    >
                      <CaretUp className="size-3.5" />
                    </ToolButton>
                    <span className="min-w-14 px-0.5 text-center text-[12px] tabular-nums text-muted-foreground">
                      {!nav.count
                        ? msg("submit.agentic_ide.review.no_file_changes")
                        : nav.pos
                          ? formatMsg("submit.agentic_ide.review.change_position", {
                              pos: nav.pos,
                              count: nav.count,
                            })
                          : formatMsg("submit.agentic_ide.review.change_count", {
                              count: nav.count,
                            })}
                    </span>
                    <ToolButton
                      label={msg("submit.agentic_ide.review.next_change")}
                      disabled={!nav.count}
                      onClick={() => navRef.current?.step(1)}
                    >
                      <CaretDown className="size-3.5" />
                    </ToolButton>
                  </span>
                )}
              {statusOf(tabFile) !== "unchanged" && (
                <ToolButton
                  label={
                    hideChanges.has(tabFile.path)
                      ? msg("submit.agentic_ide.review.show_changes")
                      : msg("submit.agentic_ide.review.hide_changes")
                  }
                  active={!hideChanges.has(tabFile.path)}
                  onClick={() =>
                    setHideChanges((prev) => {
                      const next = new Set(prev);
                      if (next.has(tabFile.path)) next.delete(tabFile.path);
                      else next.add(tabFile.path);
                      return next;
                    })
                  }
                >
                  <GitDiff className="size-4" />
                </ToolButton>
              )}
              <DropdownMenu>
                <MenuButton label={msg("submit.agentic_ide.review.file_controls")}>
                  <DotsThree className="size-4" weight="bold" />
                </MenuButton>
                <PaneMenuContent align="end" className="min-w-52">
                  <MenuRow
                    icon={<TextAlignLeft />}
                    label={msg("submit.agentic_ide.review.word_wrap")}
                    checked={wrap}
                    keepOpen
                    onSelect={() => setWrap((w) => !w)}
                  />
                  {isMarkdown(tabFile.path) && (
                    <MenuRow
                      icon={<Eye />}
                      label={msg("submit.agentic_ide.review.rich_preview")}
                      checked={rich}
                      keepOpen
                      onSelect={() => setRich((r) => !r)}
                    />
                  )}
                </PaneMenuContent>
              </DropdownMenu>
              {host && <RepoHostButton host={host} />}
              {listToggle}
            </span>
          </>
        ) : (
          <>
            <SourceLabel sum={sum} />
            <span className="ms-auto flex items-center gap-0.5">
              {inlineTools && (
                <>
                  <ToolButton
                    label={msg("submit.agentic_ide.review.word_wrap")}
                    pressed={wrap}
                    onClick={() => setWrap((w) => !w)}
                  >
                    <TextAlignLeft className="size-4" />
                  </ToolButton>
                  <ToolButton label={collapseLabel} onClick={collapseAll}>
                    <ArrowsInLineVertical className="size-4" />
                  </ToolButton>
                  <ToolButton label={modeLabel(nextMode)} onClick={() => setDiffMode(nextMode)}>
                    <ModeIcon className="size-4" />
                  </ToolButton>
                </>
              )}
              <DropdownMenu>
                <MenuButton label={msg("submit.agentic_ide.review.changes_options")}>
                  <DotsThree className="size-4" weight="bold" />
                </MenuButton>
                <PaneMenuContent align="end" className="min-w-64">
                  {!inlineTools && (
                    <>
                      <PaneLabel>{msg("submit.agentic_ide.review.view")}</PaneLabel>
                      <MenuRow
                        icon={<TextAlignLeft />}
                        label={msg("submit.agentic_ide.review.word_wrap")}
                        checked={wrap}
                        keepOpen
                        onSelect={() => setWrap((w) => !w)}
                      />
                      <MenuRow
                        icon={<ModeIcon />}
                        label={modeLabel(nextMode)}
                        keepOpen
                        onSelect={() => setDiffMode(nextMode)}
                      />
                      <MenuRow
                        icon={<ArrowsInLineVertical />}
                        label={collapseLabel}
                        onSelect={collapseAll}
                      />
                      <PaneSeparator />
                    </>
                  )}
                  <PaneLabel>{msg("submit.agentic_ide.review.diff")}</PaneLabel>
                  <MenuRow
                    icon={<FileText />}
                    label={msg("submit.agentic_ide.review.load_full_files")}
                    checked={fullFiles}
                    keepOpen
                    onSelect={() => setFullFiles((v) => !v)}
                  />
                  <MenuRow
                    icon={<Eye />}
                    label={msg("submit.agentic_ide.review.rich_preview")}
                    checked={rich}
                    keepOpen
                    onSelect={() => setRich((v) => !v)}
                  />
                  <MenuRow
                    icon={<HighlighterIcon />}
                    label={msg("submit.agentic_ide.review.word_diffs")}
                    checked={wordDiffs}
                    keepOpen
                    onSelect={() => setWordDiffs((v) => !v)}
                  />
                  <PaneSeparator />
                  <PaneLabel>{msg("submit.agentic_ide.review.hide")}</PaneLabel>
                  <MenuRow
                    icon={<ParagraphIcon />}
                    label={msg("submit.agentic_ide.review.hide_white_space")}
                    checked={ignoreSpace}
                    keepOpen
                    onSelect={() => setIgnoreSpace((v) => !v)}
                  />
                  <MenuRow
                    icon={<BracketsCurly />}
                    label={msg("submit.agentic_ide.review.hide_imports")}
                    checked={hideImports}
                    keepOpen
                    onSelect={() => setHideImports((v) => !v)}
                  />
                </PaneMenuContent>
              </DropdownMenu>
              {jumpButton}
              {listToggle}
            </span>
          </>
        )}
      </div>

      <div className="flex min-h-0 flex-1">
        {/* The diffs, or the opened file. */}
        <div className="flex min-w-0 flex-1 flex-col">
          {large && active === "changes" && changed.length > 0 && (
            <LargeDiffBanner
              index={Math.max(
                0,
                changed.findIndex((f) => f.path === largePath),
              )}
              count={changed.length}
              onStep={(i) => showDiff(changed[i]!.path)}
            />
          )}
          <div
            ref={scroller}
            id={panelId}
            role="tabpanel"
            aria-labelledby={tabId(active)}
            className="ide-scroll relative min-h-0 min-w-0 flex-1 overflow-auto"
          >
            {newPage ? (
              <NewTabPage showList={showList} onShowList={() => setShowList(true)} />
            ) : tabFile ? (
              tabView(tabFile)
            ) : !changed.length ? (
              <div className="flex h-full flex-col items-center justify-center gap-1.5 px-6 pb-16 text-center">
                <h2 className="text-[14px] font-medium text-foreground">
                  {msg("submit.agentic_ide.empty")}
                </h2>
                <p className="max-w-80 text-[13px] text-muted-foreground">
                  {msg("submit.agentic_ide.empty_hint")}
                </p>
              </div>
            ) : (
              shownChanges.map((f) => {
                const shut = collapsed.has(f.path);
                return (
                  <section
                    key={f.path}
                    ref={(el) => {
                      if (el) sections.current.set(f.path, el);
                    }}
                    className="group/file-diff"
                  >
                    <header
                      key={flash?.path === f.path ? flash.n : 0}
                      onClick={() => toggle(f.path)}
                      title={f.path}
                      className={cn(
                        "group/diff-header @container/diff-header sticky top-0 z-10 flex h-9 cursor-pointer select-none items-center gap-2 bg-[var(--surface-2)] ps-3 pe-2 transition-colors duration-150 hover:bg-[color-mix(in_oklab,var(--surface-2),var(--foreground)_3%)]",
                        flash?.path === f.path && "ide-touched",
                      )}
                    >
                      {/* The header folds on any click; this button gives the keyboard the same fold. */}
                      <button
                        type="button"
                        aria-expanded={!shut}
                        onClick={(e) => {
                          e.stopPropagation();
                          toggle(f.path);
                        }}
                        className="flex min-w-0 items-center gap-2 rounded-sm focus-visible:outline-2 focus-visible:outline-ring"
                      >
                        <FileGlyph path={f.path} />
                        {/* Full path when the header has room, the file name alone when narrow, as in Codex. */}
                        <span className="min-w-0 truncate text-[13px]" dir="ltr">
                          {dirName(f.path) && (
                            <span className="hidden text-muted-foreground @xs/diff-header:inline">
                              {dirName(f.path)}/
                            </span>
                          )}
                          <span className="font-medium text-foreground">{baseName(f.path)}</span>
                        </span>
                        <Caret
                          open={!shut}
                          className="text-muted-foreground opacity-0 transition-[opacity,transform] duration-300 ease-[cubic-bezier(0.22,1,0.36,1)] group-focus-within/diff-header:opacity-100 group-hover/diff-header:opacity-100"
                        />
                      </button>
                      <FileCounts file={f} />
                      <span
                        className="ms-auto flex shrink-0 items-center gap-0.5"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <ToolButton
                          label={msg("submit.agentic_ide.review.open_in_tab")}
                          onClick={() => openTab(f.path)}
                        >
                          <ArrowSquareOut className="size-3.5" />
                        </ToolButton>
                      </span>
                    </header>
                    {/* The body clips only while it moves, so sticky rows inside it work at rest. */}
                    <AnimatePresence initial={false}>
                      {!shut && (
                        <motion.div
                          key="body"
                          initial={{ height: 0, opacity: 0, overflow: "hidden" }}
                          animate={{
                            height: "auto",
                            opacity: 1,
                            transitionEnd: { overflow: "visible" },
                            transition: {
                              height: { duration: 0.32, ease: FOLD_EASE },
                              opacity: { duration: 0.22, delay: 0.06, ease: "easeOut" },
                            },
                          }}
                          exit={{
                            height: 0,
                            opacity: 0,
                            overflow: "hidden",
                            transition: {
                              height: { duration: 0.22, ease: FOLD_EASE },
                              opacity: { duration: 0.12, ease: "easeIn" },
                            },
                          }}
                        >
                          {body(f)}
                        </motion.div>
                      )}
                    </AnimatePresence>
                  </section>
                );
              })
            )}
          </div>
        </div>

        {/* The file list grows from the end edge while the diffs rewrap beside it. */}
        <AnimatePresence initial={false}>
          {showList && (
            <motion.aside
              key="list"
              initial={{ width: 0, opacity: 0 }}
              animate={{ width: LIST_WIDTH, opacity: 1 }}
              exit={{ width: 0, opacity: 0 }}
              transition={{
                width: { duration: 0.25, ease: LIST_EASE },
                opacity: { duration: 0.15 },
              }}
              className="shrink-0 overflow-hidden border-s border-border/70"
            >
              <div
                className="ide-scroll flex h-full flex-col gap-0.5 overflow-y-auto p-2"
                style={{ width: LIST_WIDTH }}
              >
                <label className="mb-1.5 flex h-8 shrink-0 items-center gap-1.5 rounded-full border border-border bg-background px-3 text-muted-foreground transition-colors focus-within:border-foreground/30">
                  <MagnifyingGlass className="size-3.5 shrink-0" aria-hidden />
                  <input
                    value={filter}
                    onChange={(e) => setFilter(e.target.value)}
                    onKeyDown={(e) => e.key === "Escape" && setFilter("")}
                    placeholder={msg("submit.agentic_ide.review.filter_placeholder")}
                    aria-label={msg("submit.agentic_ide.review.filter_label")}
                    className="min-w-0 flex-1 bg-transparent text-[12.5px] text-foreground outline-none placeholder:text-muted-foreground"
                  />
                  {filter && (
                    <button
                      type="button"
                      onClick={() => setFilter("")}
                      aria-label={msg("submit.agentic_ide.review.clear_filter")}
                      className="rounded p-0.5 hover:text-foreground"
                    >
                      <X className="size-3" />
                    </button>
                  )}
                </label>
                {/* An unchanged file shows nothing about changes: only the repository tree. */}
                {!plainFile && (tabFile || newPage) && (
                  <button
                    type="button"
                    onClick={() => setActive("changes")}
                    className="flex h-7 shrink-0 items-center gap-1.5 rounded-md px-1.5 text-start text-[12.5px] font-medium text-foreground transition-colors hover:bg-accent/60"
                  >
                    <ListBullets className="size-3.5 text-muted-foreground" aria-hidden />
                    {msg("submit.agentic_ide.review.all_changes")}
                  </button>
                )}
                {!plainFile && (
                  <Section
                    label={msg("submit.agentic_ide.review.changed_files")}
                    marker={matching.length > 0}
                  >
                    {matching.length ? (
                      changedRows
                    ) : (
                      <p className="px-2 py-2 text-[12.5px] text-muted-foreground">
                        {msg("submit.agentic_ide.review.no_matching")}
                      </p>
                    )}
                  </Section>
                )}
                {/* The whole repository stays browsable from Changes too; a file opens in its own tab. */}
                <Section label={msg("submit.agentic_ide.review.all_files")}>
                  <FileTree
                    root={tree}
                    selected={tabFile?.path ?? null}
                    onSelect={(path) => openTab(path, true)}
                    touched={touched}
                  />
                </Section>
              </div>
            </motion.aside>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}

function Tab({
  id,
  panelId,
  selected,
  onSelect,
  onPin,
  onClose,
  icon,
  label,
  hint,
  italic = false,
}: {
  id: string;
  panelId: string;
  selected: boolean;
  onSelect: () => void;
  onPin?: () => void;
  onClose?: () => void;
  icon: React.ReactNode;
  label: string;
  hint?: string;
  italic?: boolean;
}) {
  // The close button sits beside the tab, not inside it: a tab holds no other control.
  return (
    <div
      title={hint}
      onClick={onSelect}
      onDoubleClick={onPin}
      onAuxClick={(e) => e.button === 1 && onClose?.()}
      className={cn(
        "group/tab flex h-7 shrink-0 cursor-pointer select-none items-center gap-1.5 rounded-md ps-2.5 text-[12.5px] transition-colors duration-150",
        onClose ? "pe-1" : "pe-2.5",
        selected
          ? "min-w-32 bg-card text-foreground shadow-[0_1px_2px_rgba(19,19,19,0.08)]"
          : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
      )}
    >
      <div
        id={id}
        role="tab"
        aria-selected={selected}
        aria-controls={panelId}
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key !== "Enter" && e.key !== " ") return;
          // Space would scroll the page.
          e.preventDefault();
          onSelect();
        }}
        className="flex min-w-0 flex-1 items-center gap-1.5 rounded-sm focus-visible:outline-2 focus-visible:outline-ring"
      >
        {icon}
        <span className={cn("max-w-40 truncate", italic && "italic")} dir="ltr">
          {label}
        </span>
      </div>
      {onClose && (
        <button
          type="button"
          aria-label={formatMsg("submit.agentic_ide.review.close_tab", { name: label })}
          onClick={(e) => {
            e.stopPropagation();
            onClose();
          }}
          className={cn(
            "ms-auto flex size-5 items-center justify-center rounded text-muted-foreground transition-opacity duration-150 hover:bg-accent hover:text-foreground focus-visible:opacity-100 group-hover/tab:opacity-100",
            selected ? "opacity-100" : "opacity-0",
          )}
        >
          <X className="size-3.5" />
        </button>
      )}
    </div>
  );
}

/**
 * Codex's New tab page with no browser: an "Open file" empty state, and the
 * file tree beside it does the opening.
 */
function NewTabPage({ showList, onShowList }: { showList: boolean; onShowList: () => void }) {
  return (
    <div className="flex h-full min-h-72 flex-col items-center justify-center gap-1.5 px-6 pb-16 text-center">
      <FilesIcon className="mb-2 size-7 text-muted-foreground/70" />
      <h2 className="text-[14px] font-medium text-foreground">
        {msg("submit.agentic_ide.review.open_file")}
      </h2>
      <p className="text-[13px] text-muted-foreground">
        {msg("submit.agentic_ide.review.open_file_hint")}
      </p>
      {/* With the tree hidden, the hint would point at nothing. */}
      {!showList && (
        <button
          type="button"
          onClick={onShowList}
          className="mt-3 flex h-7 items-center gap-1.5 rounded-md border border-border px-2.5 text-[12.5px] transition-colors hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring"
        >
          <FilesIcon className="size-3.5 text-muted-foreground" />
          {msg("submit.agentic_ide.review.show_files")}
          <kbd className="font-sans text-[11px] text-muted-foreground">{`${commandKey()}⇧E`}</kbd>
        </button>
      )}
    </div>
  );
}
