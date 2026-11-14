"use client";

import * as React from "react";

import { turnState, useWizardStateOptional, type DraftFiles } from "@/features/agent-panel";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { lineCommentId } from "@/shared/ui/agent/line-comments";
import { GitDiff } from "@/shared/ui/icons";

import type { RepoSource } from "../ide/repo-files";
import { repoBaseText } from "../lib/repo-run";
import { METRIC_FILE, RUN_FILE } from "../lib/wizard-v2";
import { isChanged, keepFiles, lineMap, type DraftFile } from "./draft-changes";
import type { RepoFile } from "./review/lib";
import type { PaneComment } from "./review/parts";
import { ReviewPane, type RepoHostLink } from "./review/review-pane";

const OPTIMIZE_TOML = "optimize.toml";
// The entry files are new in the draft; the repository never holds them.
const DRAFT_ONLY = new Set([RUN_FILE, METRIC_FILE]);
// Below this width the chat and the Changes pane become two tabs.
const TABS_BELOW_PX = 1024;
const CHAT_MIN = 0.25;
const CHAT_MAX = 0.75;

type RepoRead = { text: string } | { issue: "binary" | "too_large" | "missing" };
const NO_READS: ReadonlyMap<string, RepoRead | null> = new Map();
const NO_FILES: readonly string[] = [];

/**
 * Read files from the repository, each once.
 *
 * Args:
 *   repo: The run's repository, or null.
 *   paths: The files to read.
 *
 * Returns:
 *   reads: each read file's text or issue; a path still loading has no entry.
 *     A failed read maps to null, and the next change to the paths reads it again.
 *   retry: reads the failed paths again.
 */
function useRepoReads(
  repo: RepoSource | null,
  paths: readonly string[],
): { reads: ReadonlyMap<string, RepoRead | null>; retry: () => void } {
  const [read, setRead] = React.useState<{
    repo: RepoSource | null;
    results: ReadonlyMap<string, RepoRead | null>;
  }>({
    repo,
    results: new Map(),
  });
  const requested = React.useRef<{ repo: RepoSource | null; paths: Set<string> }>({
    repo,
    paths: new Set(),
  });
  const key = paths.join("\n");
  // A bump reads the failed paths again.
  const [attempt, setAttempt] = React.useState(0);

  React.useEffect(() => {
    if (requested.current.repo !== repo) requested.current = { repo, paths: new Set() };
    if (!repo) return;
    for (const path of key ? key.split("\n") : []) {
      if (DRAFT_ONLY.has(path) || requested.current.paths.has(path)) continue;
      requested.current.paths.add(path);
      const settle = (result: RepoRead | null) =>
        setRead((prev) => {
          const results = new Map(prev.repo === repo ? prev.results : []);
          results.set(path, result);
          return { repo, results };
        });
      repo.read(path).then(settle, () => {
        // A later change to the draft's paths reads the file again; the
        // effect does not run on a render alone, so a failing read cannot loop.
        requested.current.paths.delete(path);
        settle(null);
      });
    }
  }, [repo, key, attempt]);

  const retry = React.useCallback(() => setAttempt((n) => n + 1), []);
  return { reads: read.repo === repo ? read.results : NO_READS, retry };
}

/**
 * List the repository's files once its tree arrives.
 *
 * Args:
 *   repo: The run's repository, or null.
 *
 * Returns:
 *   The file paths, sorted; none while the tree loads or when it fails.
 */
function useRepoTree(repo: RepoSource | null): readonly string[] {
  const [tree, setTree] = React.useState<{ repo: RepoSource | null; files: readonly string[] }>({
    repo,
    files: [],
  });
  React.useEffect(() => {
    if (!repo) return;
    let live = true;
    repo.tree.then(
      (index) => live && setTree({ repo, files: [...index.files].sort() }),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [repo]);
  return tree.repo === repo ? tree.files : NO_FILES;
}

function useWidth(): [(el: HTMLElement | null) => void, number] {
  const [el, setEl] = React.useState<HTMLElement | null>(null);
  const [width, setWidth] = React.useState(0);
  React.useEffect(() => {
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [el]);
  return [setEl, width];
}

/**
 * The submission step of a repository run as an agentic IDE: the setup chat
 * on one side and, from its first edit, the Changes pane on the other. The
 * pane shows what the draft changed and edits the files in place. The chat
 * header's Changes button opens and closes it.
 */
export function AgenticIde({
  chat,
  texts,
  repo,
  busy,
  onWrite,
  host,
}: {
  /** The setup agent's chat, docked here. */
  chat: React.ReactNode;
  /** The draft's files, by path. */
  texts: Readonly<Record<string, string>>;
  repo: RepoSource | null;
  /** The agent is on a turn; edits wait for it. */
  busy: boolean;
  /** Write a draft file, or drop it with null. */
  onWrite: (path: string, text: string | null) => void;
  /** The repository's page on its host, or null when the run has none. */
  host?: RepoHostLink | null;
}) {
  const agent = useWizardStateOptional();
  const setViewChanges = agent?.setViewChanges;
  const setRepoBase = agent?.setRepoBase;
  const setDraftFiles = agent?.setDraftFiles;
  const addLineComment = agent?.addLineComment;
  const moveLineComments = agent?.moveLineComments;
  const removeLineComment = agent?.removeLineComment;
  const updateLineComment = agent?.updateLineComment;
  const lineComments = agent?.lineComments;
  const setChangesToggle = agent?.setChangesToggle;

  const [setRoot, width] = useWidth();
  const rootRef = React.useRef<HTMLDivElement | null>(null);
  const tabs = width > 0 && width < TABS_BELOW_PX;
  const [tab, setTab] = React.useState<"chat" | "changes">("chat");
  const [paneOpen, setPaneOpen] = React.useState(false);
  const [chatShare, setChatShare] = React.useState(0.45);
  const [focus, setFocus] = React.useState<{ path: string; paths?: readonly string[] } | null>(
    null,
  );
  // The chat's width follows the divider at once while it is dragged, and eases otherwise.
  const [dragging, setDragging] = React.useState(false);

  const draftPaths = React.useMemo(() => Object.keys(texts).sort(), [texts]);
  const basePaths = React.useMemo(
    () =>
      repo && !draftPaths.includes(OPTIMIZE_TOML) ? [...draftPaths, OPTIMIZE_TOML] : draftPaths,
    [repo, draftPaths],
  );
  // A file of the tree that the draft does not hold is read when its tab opens.
  const [opened, setOpened] = React.useState<ReadonlySet<string>>(() => new Set());
  const readPaths = React.useMemo(
    () => [...basePaths, ...[...opened].filter((path) => !(path in texts))],
    [basePaths, opened, texts],
  );
  const { reads, retry } = useRepoReads(repo, readPaths);
  const treePaths = useRepoTree(repo);
  // The base the draft changes: "" for a file the repository cannot give as text.
  // A failed or unreadable optimize.toml read maps to undefined: the repository
  // may have the file, so the Metric screen must not hold on it (SKY-693).
  const base = React.useMemo(() => {
    const known = new Map<string, string | undefined>();
    for (const [path, result] of reads) known.set(path, repoBaseText(path, result));
    for (const path of basePaths) if (DRAFT_ONLY.has(path) || !repo) known.set(path, "");
    return known;
  }, [reads, basePaths, repo]);
  // The chat's "Edited N files" card diffs a file the draft
  // did not hold yet (the repository's optimize.toml) against this base.
  // A read with no known text has no entry, so the Metric screen does not hold on it.
  const knownBase = React.useMemo(
    () =>
      new Map(
        [...base].filter((entry): entry is [string, string] => entry[1] !== undefined),
      ) as ReadonlyMap<string, string>,
    [base],
  );
  React.useEffect(() => {
    setRepoBase?.(knownBase);
  }, [setRepoBase, knownBase]);
  React.useEffect(() => () => setRepoBase?.(null), [setRepoBase]);

  // A file whose text did not change keeps its object, so an agent write to
  // another file does not redraw the rows of the open one.
  const [fileCache, setFileCache] = React.useState<readonly DraftFile[]>([]);
  const allFiles = keepFiles(
    fileCache,
    draftPaths.map((path) => ({ path, before: base.get(path) ?? "", after: texts[path] ?? "" })),
  );
  if (allFiles !== fileCache) setFileCache(allFiles);
  // A draft file whose base is still loading shows no diff yet, not a whole new file.
  const paneFiles = React.useMemo<RepoFile[]>(() => {
    const draft = allFiles.map((f) => (base.has(f.path) ? f : { ...f, loading: true }));
    const rest = treePaths
      .filter((path) => !(path in texts))
      .map((path): RepoFile => {
        const result = reads.get(path);
        if (result === undefined) return { path, before: "", after: "", loading: true };
        if (result === null) return { path, before: "", after: "", issue: "unreadable" };
        if ("issue" in result) return { path, before: "", after: "", issue: result.issue };
        return { path, before: result.text, after: result.text };
      });
    return [...draft, ...rest];
  }, [allFiles, base, treePaths, texts, reads]);
  const anyChange = allFiles.some((f) => base.has(f.path) && isChanged(f));

  // The pane opens by itself once, on the first change; after that the user decides.
  const autoOpened = React.useRef(false);
  React.useEffect(() => {
    if (!anyChange || autoOpened.current) return;
    autoOpened.current = true;
    setPaneOpen(true);
  }, [anyChange]);

  const showChanges = React.useCallback((path?: string, paths?: readonly string[]) => {
    setPaneOpen(true);
    setTab("changes");
    if (path) setFocus({ path, paths });
  }, []);
  React.useEffect(() => {
    if (!setViewChanges) return;
    setViewChanges(showChanges);
    return () => setViewChanges(null);
  }, [setViewChanges, showChanges]);

  // The chat header holds the pane's button beside the pane; narrow screens use the tabs.
  React.useEffect(() => {
    if (!setChangesToggle) return;
    setChangesToggle(tabs ? null : { open: paneOpen, toggle: () => setPaneOpen((open) => !open) });
  }, [setChangesToggle, tabs, paneOpen]);
  React.useEffect(() => () => setChangesToggle?.(null), [setChangesToggle]);

  // A write that moves lines moves the file's comments with them, so each
  // stays on its line and the agent gets the right number. The agent's writes
  // reach only `texts`, so the move follows `texts`, not the pane's writes. A
  // layout effect moves them before the browser paints the old line.
  const prevTexts = React.useRef(texts);
  React.useLayoutEffect(() => {
    const prev = prevTexts.current;
    prevTexts.current = texts;
    if (prev === texts || !moveLineComments) return;
    for (const path of Object.keys(texts)) {
      const before = prev[path];
      const after = texts[path];
      if (before === undefined || after === undefined || before === after) continue;
      if (!(lineComments ?? []).some((c) => c.path === path)) continue;
      moveLineComments(path, lineMap(before, after));
    }
  }, [texts, moveLineComments, lineComments]);
  // A file that was not in the draft before goes away when its change is
  // undone, and its comments go with it: no line is left for them.
  const write = React.useCallback(
    (file: Pick<DraftFile, "path" | "before">, text: string) => {
      if (busy) return;
      if (text === "" && file.before === "" && !DRAFT_ONLY.has(file.path)) {
        for (const c of lineComments ?? []) if (c.path === file.path) removeLineComment?.(c);
        onWrite(file.path, null);
      } else {
        onWrite(file.path, text);
      }
    },
    [busy, onWrite, lineComments, removeLineComment],
  );

  // The chat's Undo and Reapply read and write the draft through these. A
  // write refuses when a file changed after the turn, so it never drops the user's edit.
  const draftFiles = React.useMemo<DraftFiles>(
    () => ({
      text: (path) => texts[path] ?? "",
      write: (edits, side) => {
        if (busy) return false;
        const from = side === "before" ? "applied" : "undone";
        if (turnState(edits, (path) => texts[path] ?? "") !== from) return false;
        for (const edit of edits) write(edit, side === "before" ? edit.before : edit.after);
        return true;
      },
    }),
    [texts, busy, write],
  );
  React.useEffect(() => {
    setDraftFiles?.(draftFiles);
  }, [setDraftFiles, draftFiles]);
  React.useEffect(() => () => setDraftFiles?.(null), [setDraftFiles]);

  // Stable, so the pane's lines do not redraw on every render of the IDE.
  // A user's edit that empties a new file keeps the empty file; only an undo removes it.
  const editFile = React.useCallback(
    (path: string, text: string) => {
      if (busy) return;
      onWrite(path, text);
    },
    [busy, onWrite],
  );
  const openFile = React.useCallback(
    (path: string) => {
      if (reads.get(path) === null) retry();
      setOpened((prev) => (prev.has(path) ? prev : new Set(prev).add(path)));
    },
    [reads, retry],
  );

  const comments = React.useMemo<PaneComment[]>(
    () =>
      (lineComments ?? []).map((c) => {
        return { id: lineCommentId(c), path: c.path, line: c.line, text: c.text };
      }),
    [lineComments],
  );
  const byId = React.useMemo(() => {
    const map = new Map((lineComments ?? []).map((c) => [lineCommentId(c), c]));
    return (id: string) => map.get(id);
  }, [lineComments]);
  const comment = React.useMemo(
    () =>
      addLineComment
        ? (c: Omit<PaneComment, "id">) =>
            addLineComment({ path: c.path, line: c.line, text: c.text })
        : undefined,
    [addLineComment],
  );
  const removeComment = React.useMemo(
    () =>
      removeLineComment
        ? (id: string) => {
            const c = byId(id);
            if (c) removeLineComment(c);
          }
        : undefined,
    [removeLineComment, byId],
  );
  const editComment = React.useMemo(
    () =>
      updateLineComment
        ? (id: string, text: string) => {
            const c = byId(id);
            if (c) updateLineComment(c, text);
          }
        : undefined,
    [updateLineComment, byId],
  );
  const hidePane = React.useCallback(() => (tabs ? setTab("chat") : setPaneOpen(false)), [tabs]);

  const pane = (
    <ReviewPane
      files={paneFiles}
      request={focus}
      readOnly={busy}
      onEdit={editFile}
      onOpenFile={openFile}
      comments={comments}
      onComment={comment}
      onRemoveComment={removeComment}
      onEditComment={editComment}
      onHide={hidePane}
      host={host}
    />
  );

  const startDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const box = rootRef.current?.getBoundingClientRect();
    if (!box) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
    // The drag drops the transition, so a running ease ends with no end event.
    setEasing(false);
    const rtl = getComputedStyle(rootRef.current!).direction === "rtl";
    const move = (e: PointerEvent) => {
      const share = rtl ? (box.right - e.clientX) / box.width : (e.clientX - box.left) / box.width;
      setChatShare(Math.min(CHAT_MAX, Math.max(CHAT_MIN, share)));
    };
    const target = event.currentTarget;
    const stop = () => {
      setDragging(false);
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", stop);
      target.removeEventListener("pointercancel", stop);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", stop);
    target.addEventListener("pointercancel", stop);
  };

  const nudge = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const rtl = getComputedStyle(event.currentTarget).direction === "rtl";
    const grow = (event.key === "ArrowRight") !== rtl;
    setChatShare((share) => Math.min(CHAT_MAX, Math.max(CHAT_MIN, share + (grow ? 0.02 : -0.02))));
  };

  const tabClass = (active: boolean) =>
    cn(
      "flex h-7 cursor-pointer items-center gap-1.5 rounded-md px-2.5 text-xs font-medium transition-colors",
      active ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground",
    );
  const showPane = tabs ? tab === "changes" : paneOpen;
  const ease = "duration-[340ms] ease-[cubic-bezier(0.16,1,0.3,1)] motion-reduce:duration-150";
  // The pane's content keeps its open width, so it slides out from the edge
  // while the chat narrows, and does not reflow on every frame.
  // Less the 1px separator beside it.
  const paneWidth = !tabs && width > 0 ? Math.round(width * (1 - chatShare)) - 1 : undefined;
  // After the close fade, the content leaves the layout, so a clipped pane
  // does not keep drawing the open file.
  const [paneGone, setPaneGone] = React.useState(!showPane);
  if (showPane && paneGone) setPaneGone(false);
  // The tabs hide the pane at once, with no fade to wait for.
  if (!showPane && tabs && !paneGone) setPaneGone(true);
  // Widths ease only when the pane opens, closes or is nudged. A window
  // resize moves the percent-wide chat at once, so the content must too.
  const layout = `${tabs}|${showPane}|${chatShare}`;
  const [easedLayout, setEasedLayout] = React.useState(layout);
  const [easing, setEasing] = React.useState(false);
  if (easedLayout !== layout) {
    setEasedLayout(layout);
    // The tabs have no width to ease, and the chat's width out of them starts at
    // auto, which does not animate: no transition would end either ease.
    setEasing(!dragging && !tabs && easedLayout.startsWith("false|"));
  }
  // Under reduced motion the widths move at once; the pane's fade stays.
  const widthEase =
    !tabs && easing && !dragging && cn("transition-[width] motion-reduce:transition-none", ease);

  return (
    <div
      ref={(el) => {
        rootRef.current = el;
        setRoot(el);
      }}
      className="flex h-full min-h-0 flex-col"
    >
      {tabs && (
        <div
          className="flex h-10 shrink-0 items-center gap-1 border-b border-border/60 px-2"
          role="tablist"
        >
          <button
            type="button"
            role="tab"
            aria-selected={tab === "chat"}
            className={tabClass(tab === "chat")}
            onClick={() => setTab("chat")}
          >
            {msg("submit.agentic_ide.chat_tab")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "changes"}
            className={tabClass(tab === "changes")}
            onClick={() => setTab("changes")}
          >
            <GitDiff className="size-3.5" aria-hidden />
            {msg("submit.agentic_ide.changes_tab")}
          </button>
        </div>
      )}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        <section
          aria-label={msg("submit.agentic_ide.chat_tab")}
          className={cn(
            "relative min-w-0",
            tabs ? cn("flex-1", tab !== "chat" && "hidden") : "shrink-0",
            widthEase,
          )}
          style={tabs ? undefined : { width: showPane ? `${chatShare * 100}%` : "100%" }}
          onTransitionEnd={(e) => {
            if (e.target === e.currentTarget) setEasing(false);
          }}
        >
          {chat}
        </section>
        {showPane && !tabs && (
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label={msg("submit.agentic_ide.resize")}
            aria-valuemin={CHAT_MIN * 100}
            aria-valuemax={CHAT_MAX * 100}
            aria-valuenow={Math.round(chatShare * 100)}
            tabIndex={0}
            onPointerDown={startDrag}
            onKeyDown={nudge}
            className="relative w-px shrink-0 cursor-col-resize bg-border/70 outline-none after:absolute after:inset-y-0 after:-inset-x-1.5 after:content-[''] hover:bg-primary/50 focus-visible:bg-primary"
          />
        )}
        <section
          aria-label={msg("submit.agentic_ide.changes_tab")}
          inert={!showPane}
          onTransitionEnd={(e) => {
            if (e.target === e.currentTarget && !showPane) setPaneGone(true);
          }}
          className={cn(
            "relative flex min-w-0 flex-1 justify-end overflow-hidden",
            tabs ? !showPane && "hidden" : cn("transition-opacity", ease),
            !tabs && !showPane && "opacity-0",
          )}
        >
          <div
            className={cn(
              "h-full min-w-0 flex-1 shrink-0",
              widthEase,
              !tabs && paneGone && "hidden",
            )}
            style={paneWidth ? { width: paneWidth, flex: "none" } : undefined}
          >
            {pane}
          </div>
        </section>
      </div>
    </div>
  );
}
