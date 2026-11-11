"use client";

import * as React from "react";
import { toast } from "react-toastify";

import { useWizardStateOptional, type FileEdit } from "@/features/agent-panel";
import { checkOptimizeToml } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { GitDiff } from "@/shared/ui/icons";

import type { RepoSource } from "../ide/repo-files";
import { METRIC_FILE, RUN_FILE } from "../lib/wizard-v2";
import { ChangesPane, type ChangeScope } from "./ChangesPane";
import { revertHunk, type DraftFile } from "./draft-changes";
import type { LineMarker } from "./FileDiff";
import { SetupCheckConsole } from "./SetupCheckConsole";
import { checkFeedback, errorLine, setupCheckState, type SetupCheckRun } from "./setup-check";

const OPTIMIZE_TOML = "optimize.toml";
// The entry files are new in the draft; the repository never holds them.
const DRAFT_ONLY = new Set([RUN_FILE, METRIC_FILE]);
// Below this width the chat and the Changes pane become two tabs.
const TABS_BELOW_PX = 1024;
const CHAT_MIN = 0.25;
const CHAT_MAX = 0.75;

/** Where the editor opens: a file, and a line in it. */
export interface OpenRequest {
  path: string;
  line?: number;
}

/**
 * Read each file's text in the repository, the base the draft changes.
 *
 * Args:
 *   repo: The run's repository, or null.
 *   paths: The files to read.
 *
 * Returns:
 *   Each read file's text ("" when the repository has none); a path still
 *   loading has no entry.
 */
function useRepoBase(
  repo: RepoSource | null,
  paths: readonly string[],
): ReadonlyMap<string, string> {
  const [read, setRead] = React.useState<{
    repo: RepoSource | null;
    texts: ReadonlyMap<string, string>;
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
      const settle = (text: string) =>
        setRead((prev) => {
          const texts = new Map(prev.repo === repo ? prev.texts : []);
          texts.set(path, text);
          return { repo, texts };
        });
      repo.read(path).then(
        (result) => settle("text" in result ? result.text : ""),
        () => settle(""),
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
 * pane shows what the last turn or the whole draft changed, reverts files and
 * hunks, runs the setup check, and opens the editor.
 */
export function AgenticIde({
  chat,
  texts,
  repo,
  busy,
  onWrite,
  renderEditor,
}: {
  /** The setup agent's chat, docked here. */
  chat: React.ReactNode;
  /** The draft's files, by path. */
  texts: Readonly<Record<string, string>>;
  repo: RepoSource | null;
  /** The agent is on a turn; reverts wait for it. */
  busy: boolean;
  /** Write a draft file, or drop it with null. */
  onWrite: (path: string, text: string | null) => void;
  /** The workbench, opened at a file and line. */
  renderEditor: (request: OpenRequest | null) => React.ReactNode;
}) {
  const agent = useWizardStateOptional();
  const lastTurn: readonly FileEdit[] = agent?.lastTurnEdits ?? [];
  const setViewChanges = agent?.setViewChanges;
  const setSetupCheckIssue = agent?.setSetupCheckIssue;
  const setRepoBase = agent?.setRepoBase;

  const [setRoot, width] = useWidth();
  const rootRef = React.useRef<HTMLDivElement | null>(null);
  const tabs = width > 0 && width < TABS_BELOW_PX;
  const [tab, setTab] = React.useState<"chat" | "changes">("chat");
  const [paneOpen, setPaneOpen] = React.useState(false);
  const [chatShare, setChatShare] = React.useState(0.45);
  const [scope, setScope] = React.useState<ChangeScope>("last");
  const [focus, setFocus] = React.useState<{ path: string } | null>(null);
  const [editorOpen, setEditorOpen] = React.useState(false);
  const [openRequest, setOpenRequest] = React.useState<OpenRequest | null>(null);
  // The workbench mounts on the first Open in editor and then stays, hidden, so it never reboots.
  const [editorMounted, setEditorMounted] = React.useState(false);

  const draftPaths = React.useMemo(() => Object.keys(texts).sort(), [texts]);
  const basePaths = React.useMemo(
    () =>
      repo && !draftPaths.includes(OPTIMIZE_TOML) ? [...draftPaths, OPTIMIZE_TOML] : draftPaths,
    [repo, draftPaths],
  );
  const base = useRepoBase(repo, basePaths);
  // The chat's "Edited N files" card and "Last turn" diff a file the draft
  // did not hold yet (the repository's optimize.toml) against this base.
  React.useEffect(() => {
    setRepoBase?.(base);
  }, [setRepoBase, base]);
  React.useEffect(() => () => setRepoBase?.(null), [setRepoBase]);

  const lastFiles = React.useMemo<DraftFile[]>(
    () =>
      lastTurn.map((edit) => ({
        path: edit.path,
        before: edit.before,
        after: texts[edit.path] ?? "",
      })),
    [lastTurn, texts],
  );
  const allFiles = React.useMemo<DraftFile[]>(
    () =>
      draftPaths.map((path) => ({ path, before: base.get(path) ?? "", after: texts[path] ?? "" })),
    [draftPaths, base, texts],
  );
  const files = scope === "last" ? lastFiles : allFiles;
  const loading = React.useMemo(
    () => new Set(scope === "all" ? draftPaths.filter((path) => !base.has(path)) : []),
    [scope, draftPaths, base],
  );
  const anyChange =
    lastFiles.some((f) => f.before !== f.after) ||
    allFiles.some((f) => base.has(f.path) && f.before !== f.after);

  // The pane opens by itself once, on the first change; after that the user decides.
  const autoOpened = React.useRef(false);
  React.useEffect(() => {
    if (!anyChange || autoOpened.current) return;
    autoOpened.current = true;
    setPaneOpen(true);
  }, [anyChange]);

  const showChanges = React.useCallback((path?: string) => {
    setPaneOpen(true);
    setTab("changes");
    setEditorOpen(false);
    setScope("last");
    if (path) setFocus({ path });
  }, []);
  React.useEffect(() => {
    if (!setViewChanges) return;
    setViewChanges(showChanges);
    return () => setViewChanges(null);
  }, [setViewChanges, showChanges]);

  // The setup check reads the draft's optimize.toml, or the repository's when the draft has none.
  const tomlText = texts[OPTIMIZE_TOML] ?? base.get(OPTIMIZE_TOML) ?? "";
  const [run, setRun] = React.useState<SetupCheckRun | null>(null);
  const [running, setRunning] = React.useState(false);
  const [requestFailed, setRequestFailed] = React.useState(false);
  const [consoleOpen, setConsoleOpen] = React.useState(false);
  const checkState = setupCheckState(run, running, tomlText);

  const runCheck = React.useCallback(async () => {
    const text = tomlText;
    setRunning(true);
    setRequestFailed(false);
    setConsoleOpen(true);
    try {
      const result = await checkOptimizeToml(text);
      setRun({ text, ok: result.ok, errors: result.errors });
      if (result.ok) setConsoleOpen(false);
    } catch {
      setRequestFailed(true);
    } finally {
      setRunning(false);
    }
  }, [tomlText]);

  const failedRun = checkState === "failed" ? run : null;
  React.useEffect(() => {
    if (!setSetupCheckIssue) return;
    setSetupCheckIssue(
      failedRun
        ? {
            count: failedRun.errors.length,
            feedback: checkFeedback(failedRun, msg("submit.agentic_ide.check.feedback_intro")),
          }
        : null,
    );
  }, [setSetupCheckIssue, failedRun]);
  React.useEffect(() => () => setSetupCheckIssue?.(null), [setSetupCheckIssue]);

  const markers = React.useMemo(() => {
    const map = new Map<string, LineMarker[]>();
    if (!failedRun) return map;
    const lines: LineMarker[] = [];
    for (const error of failedRun.errors) {
      const line = errorLine(failedRun.text, error);
      if (line !== null) lines.push({ line, message: error.message });
    }
    map.set(OPTIMIZE_TOML, lines);
    return map;
  }, [failedRun]);

  // A file the draft does not hold opens read-only from the repository's folder.
  const openEditor = React.useCallback(
    (path: string, line?: number) => {
      const inDraft = path in texts;
      setOpenRequest({ path: inDraft || !repo ? path : `${repo.folder}/${path}`, line });
      setEditorOpen(true);
      setEditorMounted(true);
      setPaneOpen(true);
      setTab("changes");
    },
    [texts, repo],
  );

  // A file that was not in the draft before goes away when its change is undone.
  const write = (file: DraftFile, text: string) => {
    if (busy) return;
    if (text === "" && file.before === "" && !DRAFT_ONLY.has(file.path)) onWrite(file.path, null);
    else onWrite(file.path, text);
  };

  const copyPath = (path: string) => {
    void navigator.clipboard
      ?.writeText(path)
      .then(() => toast(msg("submit.agentic_ide.path_copied")));
  };

  const pane = (
    <ChangesPane
      scope={scope}
      onScope={setScope}
      files={files}
      loading={loading}
      focus={focus}
      locked={busy}
      markers={markers}
      checkState={checkState}
      checkErrors={run?.errors.length ?? 0}
      onRunCheck={() => void runCheck()}
      onRevertFile={(file) => write(file, file.before)}
      onRevertHunk={(file, hunk) => write(file, revertHunk(file.before, file.after, hunk))}
      onOpenEditor={openEditor}
      onCopyPath={copyPath}
      editorOpen={editorOpen}
      editor={editorMounted ? renderEditor(openRequest) : null}
      onBackToChanges={() => setEditorOpen(false)}
      console={
        <SetupCheckConsole
          run={run}
          state={checkState}
          requestFailed={requestFailed}
          open={consoleOpen}
          onToggle={() => setConsoleOpen((open) => !open)}
          onOpenLine={(line) => openEditor(OPTIMIZE_TOML, line)}
        />
      }
    />
  );

  const totals = allFiles.reduce(
    (sum, file) => (file.before === file.after || !base.has(file.path) ? sum : sum + 1),
    0,
  );

  const startDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const box = rootRef.current?.getBoundingClientRect();
    if (!box) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const rtl = getComputedStyle(rootRef.current!).direction === "rtl";
    const move = (e: PointerEvent) => {
      const share = rtl ? (box.right - e.clientX) / box.width : (e.clientX - box.left) / box.width;
      setChatShare(Math.min(CHAT_MAX, Math.max(CHAT_MIN, share)));
    };
    const target = event.currentTarget;
    const stop = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", stop);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", stop);
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
  const changesLabel = (
    <>
      <GitDiff className="size-3.5" aria-hidden />
      {msg("submit.agentic_ide.changes_tab")}
      {totals > 0 && (
        <span className="rounded-full bg-muted-foreground/15 px-1.5 text-[0.625rem] tabular-nums">
          {totals}
        </span>
      )}
    </>
  );
  const showPane = tabs ? tab === "changes" : paneOpen;

  return (
    <div
      ref={(el) => {
        rootRef.current = el;
        setRoot(el);
      }}
      className="flex h-full min-h-0 flex-col"
    >
      <div
        className="flex h-10 shrink-0 items-center gap-1 border-b border-border/60 px-2"
        role={tabs ? "tablist" : undefined}
      >
        {tabs ? (
          <>
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
              {changesLabel}
            </button>
          </>
        ) : (
          <button
            type="button"
            aria-pressed={paneOpen}
            className={cn(tabClass(paneOpen), "ms-auto")}
            onClick={() => setPaneOpen((open) => !open)}
          >
            {changesLabel}
          </button>
        )}
      </div>
      <div className="flex min-h-0 flex-1">
        <section
          aria-label={msg("submit.agentic_ide.chat_tab")}
          className={cn(
            "relative min-w-0",
            tabs && tab !== "chat" && "hidden",
            !showPane || tabs ? "flex-1" : "shrink-0",
          )}
          style={showPane && !tabs ? { width: `${chatShare * 100}%` } : undefined}
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
          className={cn("relative min-w-0 flex-1", !showPane && "hidden")}
        >
          {pane}
        </section>
      </div>
    </div>
  );
}
