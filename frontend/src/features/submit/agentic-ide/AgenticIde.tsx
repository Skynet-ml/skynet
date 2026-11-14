"use client";

import * as React from "react";

import { turnState, useWizardStateOptional, type DraftFiles } from "@/features/agent-panel";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { GitDiff } from "@/shared/ui/icons";

import type { RepoSource } from "../ide/repo-files";
import { repoBaseText } from "../lib/repo-run";
import { METRIC_FILE, RUN_FILE } from "../lib/wizard-v2";
import { ChangesPane } from "./ChangesPane";
import { isChanged, keepFiles, lineMap, type DraftFile } from "./draft-changes";

const OPTIMIZE_TOML = "optimize.toml";
// The entry files are new in the draft; the repository never holds them.
const DRAFT_ONLY = new Set([RUN_FILE, METRIC_FILE]);
// Below this width the chat and the Changes pane become two tabs.
const TABS_BELOW_PX = 1024;
const CHAT_MIN = 0.25;
const CHAT_MAX = 0.75;

/**
 * Read each file's text in the repository, the base the draft changes.
 *
 * Args:
 *   repo: The run's repository, or null.
 *   paths: The files to read.
 *
 * Returns:
 *   Each read file's text ("" when the repository has none); a path still
 *   loading has no entry. A failed or unreadable optimize.toml read maps to
 *   undefined: the repository may have the file, so the Metric screen must
 *   not hold on it (SKY-693).
 */
function useRepoBase(
  repo: RepoSource | null,
  paths: readonly string[],
): ReadonlyMap<string, string | undefined> {
  const [read, setRead] = React.useState<{
    repo: RepoSource | null;
    texts: ReadonlyMap<string, string | undefined>;
  }>({
    repo,
    texts: new Map(),
  });
  const requested = React.useRef<{ repo: RepoSource | null; paths: Set<string> }>({
    repo,
    paths: new Set(),
  });
  const key = paths.join("\n");

  React.useEffect(() => {
    if (requested.current.repo !== repo) requested.current = { repo, paths: new Set() };
    if (!repo) return;
    for (const path of key ? key.split("\n") : []) {
      if (DRAFT_ONLY.has(path) || requested.current.paths.has(path)) continue;
      requested.current.paths.add(path);
      const settle = (text: string | undefined) =>
        setRead((prev) => {
          const texts = new Map(prev.repo === repo ? prev.texts : []);
          texts.set(path, text);
          return { repo, texts };
        });
      repo.read(path).then(
        (result) => settle(repoBaseText(path, result)),
        () => {
          // A later change to the draft's paths reads the file again; the
          // effect does not run on a render alone, so a failing read cannot loop.
          requested.current.paths.delete(path);
          settle(repoBaseText(path, null));
        },
      );
    }
  }, [repo, key]);

  return React.useMemo(() => {
    const texts = new Map(read.repo === repo ? read.texts : []);
    for (const path of paths) if (DRAFT_ONLY.has(path) || !repo) texts.set(path, "");
    return texts;
  }, [read, repo, paths]);
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
}) {
  const agent = useWizardStateOptional();
  const setViewChanges = agent?.setViewChanges;
  const setRepoBase = agent?.setRepoBase;
  const setDraftFiles = agent?.setDraftFiles;
  const addLineComment = agent?.addLineComment;
  const moveLineComments = agent?.moveLineComments;
  const removeLineComment = agent?.removeLineComment;
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
  const base = useRepoBase(repo, basePaths);
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
  const loading = React.useMemo(
    () => new Set(draftPaths.filter((path) => !base.has(path))),
    [draftPaths, base],
  );
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
  const editFile = React.useCallback(
    (file: DraftFile, text: string) => {
      if (busy) return;
      onWrite(file.path, text);
    },
    [busy, onWrite],
  );
  const comment = React.useMemo(
    () =>
      addLineComment
        ? (path: string, line: number, text: string) => addLineComment({ path, line, text })
        : undefined,
    [addLineComment],
  );

  const pane = (
    <ChangesPane
      files={allFiles}
      loading={loading}
      focus={focus}
      locked={busy}
      // A user's edit that empties a new file keeps the empty file; only an undo removes it.
      onEditFile={editFile}
      comments={agent?.lineComments ?? []}
      onDeleteComment={agent?.removeLineComment}
      onComment={comment}
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
