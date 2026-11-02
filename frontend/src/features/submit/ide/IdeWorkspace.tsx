"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { contractCheckMessage } from "@/shared/lib/i18n";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/primitives/button";
import {
  ArrowsIn,
  ArrowsOut,
  CheckCircle,
  CircleNotch,
  ListChecks,
  XCircle,
} from "@/shared/ui/icons";
import type {
  ContractCheckItem,
  ContractCheckResponse,
  FileVerification,
} from "@/shared/types/unified-run";
import { contractConsole, type ContractConsole } from "../lib/contract-console";
import { blankEntryFile, diffWorkspace, workspaceTexts } from "../lib/ide-workspace";
import { METRIC_FILE, RUN_FILE } from "../lib/wizard-v2";
import { v2Msg } from "../components/v2/ArtifactConfirmCard";
import { pendingOpens, repoOpenTarget, type IdeOpenRequest, type IdeOpenTarget } from "./open-file";
import { park, unpark } from "./park";
import type { RepoSource } from "./repo-files";
import { guardClippingAncestors, guardClippingInside } from "./scroll-guard";
import type { IdeWorkbench } from "./workbench";
import { ZOOM_EASING, ZOOM_MS, prefersReducedMotion, zoomFrames } from "./zoom";

/**
 * Open one file in the editor, keeping the keyboard where it is. A repository
 * file opens once its tree lists it, on a line clamped to its text.
 *
 * Args:
 *   workbench: The booted workbench.
 *   repo: The run's repository, or null.
 *   target: The workspace file, or the repository path, to open.
 */
async function openIn(workbench: IdeWorkbench, repo: RepoSource | null, target: IdeOpenTarget): Promise<void> {
  if (!target.inRepo) return workbench.openFile(target.path, target.line, true);
  if (!repo || !(await repo.tree).files.has(target.path)) return;
  const read = await repo.read(target.path);
  const { path, line } = repoOpenTarget(target, repo.folder, "text" in read ? read.text : null);
  await workbench.openFile(path, line, true);
}

function isDark(): boolean {
  return document.documentElement.classList.contains("dark");
}

/** One failed check as a line under its file. */
function checkLine(item: ContractCheckItem, guided: boolean): string {
  const where =
    item.sample_index != null
      ? `${v2Msg("submit.v2.metric.check.sample_title", guided, { index: item.sample_index + 1 })}: `
      : "";
  const head = `${where}${contractCheckMessage(item)}`;
  return item.detail ? `${head}\n${item.detail}` : head;
}

/** What the last Check found in one file. */
interface FileCheck {
  path: string;
  console: ContractConsole;
}

function CheckResults({
  results,
  failed,
  emptyFile,
  guided,
  stale,
  onOpen,
}: {
  results: FileCheck[];
  failed: boolean;
  /** The entry file that was empty, so the check was not sent. */
  emptyFile: string | null;
  guided: boolean;
  /** The results predate the files on screen; said beside them. */
  stale: boolean;
  onOpen: (path: string) => void;
}) {
  if (emptyFile) {
    return (
      <p role="alert" className="px-4 py-2 text-xs text-destructive">
        {v2Msg("submit.v2.ide.check.empty", guided, { path: emptyFile })}
      </p>
    );
  }
  if (failed) {
    return (
      <p role="alert" className="px-4 py-2 text-xs text-destructive">
        {v2Msg("submit.v2.ide.check.failed", guided)}
      </p>
    );
  }
  // A clean check is one line, so its arrival barely moves the editor.
  const clean = results.every(({ console: result }) => result.errors.length === 0);
  const staleNote = stale && (
    <p className={cn("px-4 text-xs text-muted-foreground", clean ? "py-2" : "pt-2")}>
      {v2Msg("submit.v2.metric.check.stale", guided)}
    </p>
  );
  if (clean) {
    return (
      <div className="flex flex-wrap items-center text-xs">
        <ul className="flex flex-wrap items-center">
          {results.map(({ path, console: result }) => (
            <li key={path} className="px-4 py-2">
              <button
                type="button"
                onClick={() => onOpen(path)}
                className="flex items-center gap-2 font-mono hover:underline"
                dir="ltr"
              >
                {result.valid ? (
                  <CheckCircle className="size-4 text-[var(--success)]" aria-hidden />
                ) : (
                  <XCircle className="size-4 text-destructive" aria-hidden />
                )}
                {path}
                <span className="font-sans text-muted-foreground">
                  {result.valid
                    ? v2Msg("submit.v2.ide.check.passed", guided)
                    : v2Msg("submit.v2.metric.files.errors", guided, {
                        count: result.errors.length,
                      })}
                </span>
              </button>
            </li>
          ))}
        </ul>
        {staleNote}
      </div>
    );
  }
  return (
    <>
      {staleNote}
      <ul className="max-h-48 divide-y divide-border/40 overflow-y-auto text-xs">
        {results.map(({ path, console: result }) => (
          <li key={path} className="px-4 py-2">
            <button
              type="button"
              onClick={() => onOpen(path)}
              className="flex items-center gap-2 font-mono hover:underline"
              dir="ltr"
            >
              {result.valid ? (
                <CheckCircle className="size-4 text-[var(--success)]" aria-hidden />
              ) : (
                <XCircle className="size-4 text-destructive" aria-hidden />
              )}
              {path}
              <span className="font-sans text-muted-foreground">
                {result.valid
                  ? v2Msg("submit.v2.ide.check.passed", guided)
                  : v2Msg("submit.v2.metric.files.errors", guided, { count: result.errors.length })}
              </span>
            </button>
            {result.errors.length > 0 && (
              <pre dir="ltr" className="mt-1 whitespace-pre-wrap ps-6 font-mono text-destructive">
                {result.errors.join("\n\n")}
              </pre>
            )}
          </li>
        ))}
      </ul>
    </>
  );
}

/**
 * The Metric step's workspace as VS Code's workbench: explorer, tabs, search
 * and the command palette over the wizard's own files, with the coding agent
 * docked beside it. Full screen moves the page's one workbench into a
 * pop-up and back, so nothing reloads. Edits go straight into the wizard's
 * state, and the wizard's changes (the agent writing a file) show up here.
 */
export function IdeWorkspace({
  runCode,
  metricCode,
  files,
  onFileChange,
  onFileDelete,
  onCheck,
  checkPhase,
  checkResult,
  agentResults,
  resultsStale,
  repo,
  openRequests,
  strip,
  sidePanel,
  guided,
}: {
  runCode: string;
  metricCode: string;
  files: Readonly<Record<string, string>>;
  onFileChange: (path: string, text: string) => void;
  onFileDelete: (path: string) => void;
  /** Run Check; null for a repository run, whose scorer is checked when it is submitted. */
  onCheck: (() => Promise<ContractCheckResponse | null>) | null;
  /** The step the running Check stands in, said beside its spinner. */
  checkPhase?: string | null;
  /** The last Check (or the static check while it is fresher); each file gets its line. */
  checkResult: ContractCheckResponse | null;
  /** The coding agent's verify_code verdict per file; shown instead of `checkResult` when set. */
  agentResults: FileVerification[] | null;
  /** The shown results predate the files (or setup) on screen. */
  resultsStale: boolean;
  /** A repository run's checkout, shown read-only in the explorer. */
  repo: RepoSource | null;
  /** Every file the coding agent (or its transcript) asked to show, oldest first; each new one opens. */
  openRequests: readonly IdeOpenRequest[];
  /** Shown above the editor (the metric model row). */
  strip?: ReactNode;
  /** The end-side region: the coding agent's chat. */
  sidePanel?: ReactNode;
  guided: boolean;
}) {
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  const [workbench, setWorkbench] = useState<IdeWorkbench | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [fullScreen, setFullScreen] = useState(false);
  const [closing, setClosing] = useState(false);
  // The inline slot (the section, or its placeholder while full screen) and
  // the pop-up layer, for the grow and shrink between them.
  const slotRef = useRef<HTMLElement | null>(null);
  const layerRef = useRef<HTMLDivElement | null>(null);
  const growFrom = useRef<DOMRect | null>(null);
  const [checking, setChecking] = useState(false);
  // What the toolbar's own Check found that the wizard's result cannot say,
  // until the next result (the agent's own Check, say) replaces it.
  const [issue, setIssue] = useState<{
    after: ContractCheckResponse | null;
    emptyFile?: string;
    failed?: boolean;
  } | null>(null);
  const checkIssue = issue && issue.after === checkResult ? issue : null;

  const texts = useMemo(
    () => workspaceTexts(runCode, metricCode, files),
    [runCode, metricCode, files],
  );
  // The IDE reports the whole workspace after each edit; the wizard gets only
  // what differs from what it holds now.
  const latest = useRef(texts);
  const sinks = useRef({ onFileChange, onFileDelete, guided });
  useLayoutEffect(() => {
    latest.current = texts;
    sinks.current = { onFileChange, onFileDelete, guided };
  });

  useEffect(() => {
    let cancelled = false;
    // The workbench outlives this step, so a refusal reads the current mode.
    const describe = (issue: string) => v2Msg(issue, sinks.current.guided);
    import("./workbench")
      .then(({ bootWorkbench }) => bootWorkbench(isDark(), describe))
      .then((booted) => {
        if (!cancelled) setWorkbench(booted);
      })
      .catch((error: unknown) => {
        console.error("IDE workbench failed to load", error);
        if (!cancelled) setLoadFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!workbench) return;
    workbench.load(latest.current);
    workbench.onEdit((next) => {
      const { writes, deletes } = diffWorkspace(latest.current, next);
      // Keep the ref ahead of the re-render, so a second edit before it
      // lands diffs against this one.
      latest.current = next;
      for (const [path, text] of writes) sinks.current.onFileChange(path, text);
      for (const path of deletes) sinks.current.onFileDelete(path);
    });
    const theme = new MutationObserver(() => workbench.setDark(isDark()));
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    workbench.setDark(isDark());
    return () => {
      theme.disconnect();
      workbench.onEdit(null);
    };
  }, [workbench]);

  // Full screen renders a new host; the one workbench moves into it, and back.
  useEffect(() => {
    if (!workbench || !host) return;
    unpark(workbench.element, host);
    workbench.layout();
    // Every size change of the host (the results strip appearing, the window,
    // the move into the pop-up) re-lays VS Code out to it; a workbench left at
    // its old size overflows the host. A hidden step measures 0 and is skipped:
    // laying out at 0 squeezes the explorer to its minimum.
    const observer = new ResizeObserver(([entry]) => {
      if (entry && entry.contentRect.width > 0 && entry.contentRect.height > 0) workbench.layout();
    });
    observer.observe(host);
    const releaseScroll = guardClippingAncestors(host);
    const releaseInside = guardClippingInside(host);
    return () => {
      releaseInside();
      releaseScroll();
      observer.disconnect();
      // VS Code boots once per page, so between hosts it waits in the
      // document, fully hidden.
      park(workbench.element, document.body);
    };
  }, [workbench, host]);

  useEffect(() => {
    workbench?.load(texts);
  }, [workbench, texts]);

  useEffect(() => {
    workbench?.setRepo(repo);
  }, [workbench, repo]);

  // After the load above, so a file the agent wrote in the same turn is
  // already in the IDE; the keyboard stays in the agent's composer.
  // One after another, so the last file asked for ends up in front.
  const opened = useRef(0);
  const opening = useRef(Promise.resolve());
  useEffect(() => {
    if (!workbench) return;
    const targets = pendingOpens(openRequests, opened.current, latest.current, repo !== null);
    opened.current = openRequests.length;
    for (const target of targets) {
      opening.current = opening.current
        .then(() => openIn(workbench, repo, target))
        .catch((error: unknown) => console.error("IDE failed to open a file", error));
    }
  }, [workbench, openRequests, repo]);
  useEffect(() => () => workbench?.setRepo(null), [workbench]);

  const toggleFullScreen = useCallback(async () => {
    await workbench?.saveAll();
    if (!fullScreen) {
      growFrom.current = slotRef.current?.getBoundingClientRect() ?? null;
      setFullScreen(true);
      return;
    }
    const layer = layerRef.current;
    const slot = slotRef.current?.getBoundingClientRect();
    if (layer && slot && typeof layer.animate === "function" && !prefersReducedMotion()) {
      setClosing(true);
      const frames = zoomFrames(slot, window.innerWidth, window.innerHeight).reverse();
      // Held at the slot until the layer unmounts, so it never flashes back to full.
      const shrink = layer.animate(frames, { duration: ZOOM_MS, easing: ZOOM_EASING, fill: "forwards" });
      await shrink.finished.catch(() => undefined);
      setClosing(false);
    }
    setFullScreen(false);
  }, [workbench, fullScreen]);

  // The layer is laid out full size at once; only its clip grows from the slot.
  useLayoutEffect(() => {
    if (!fullScreen) return;
    const from = growFrom.current;
    growFrom.current = null;
    const layer = layerRef.current;
    if (!layer || !from || typeof layer.animate !== "function" || prefersReducedMotion()) return;
    const grow = layer.animate(zoomFrames(from, window.innerWidth, window.innerHeight), {
      duration: ZOOM_MS,
      easing: ZOOM_EASING,
    });
    grow.finished.then(() => workbench?.layout()).catch(() => undefined);
    return () => grow.cancel();
  }, [fullScreen, workbench]);

  const runCheck = useCallback(async () => {
    if (!onCheck) return;
    setChecking(true);
    try {
      await workbench?.saveAll();
      const emptyFile = blankEntryFile(
        latest.current[RUN_FILE] ?? "",
        latest.current[METRIC_FILE] ?? "",
      );
      if (emptyFile) {
        setIssue({ after: checkResult, emptyFile });
        return;
      }
      setIssue(null);
      if (!(await onCheck())) setIssue({ after: checkResult, failed: true });
    } finally {
      setChecking(false);
    }
  }, [workbench, onCheck, checkResult]);

  const extraPaths = useMemo(() => Object.keys(files).sort(), [files]);
  const results = useMemo<FileCheck[]>(() => {
    if (agentResults) {
      return agentResults
        .filter(({ path, ok }) => path === RUN_FILE || path === METRIC_FILE || !ok)
        .map(({ path, ok, errors, warnings }) => ({ path, console: { valid: ok, errors, warnings } }));
    }
    if (!checkResult) return [];
    const out: FileCheck[] = [];
    for (const path of [RUN_FILE, METRIC_FILE, ...extraPaths]) {
      const result = contractConsole(checkResult, path, (item) => checkLine(item, guided), extraPaths);
      // The entry files always get a line; a helper file only when it has a problem.
      if (result && (path === RUN_FILE || path === METRIC_FILE || !result.valid))
        out.push({ path, console: result });
    }
    return out;
  }, [agentResults, checkResult, extraPaths, guided]);
  const showResults = checkIssue !== null || results.length > 0;

  const checkLabel = checking
    ? v2Msg("submit.v2.metric.check.running", guided)
    : v2Msg("submit.v2.metric.check.run", guided);
  const fullScreenLabel = fullScreen
    ? v2Msg("submit.v2.ide.exit_full_screen", guided)
    : v2Msg("submit.v2.ide.full_screen", guided);

  const body = (
    <>
      <header className="flex shrink-0 items-center gap-3 border-b border-border/60 px-4 py-2">
        <div className="ms-auto flex shrink-0 items-center gap-1">
          {onCheck && checking && checkPhase && (
            <span className="text-xs text-muted-foreground" aria-live="polite">
              {checkPhase}
            </span>
          )}
          {onCheck && (
            <Button
              size="icon-sm"
              variant="ghost"
              onClick={runCheck}
              disabled={!workbench || checking}
              aria-label={checkLabel}
            >
              {checking ? (
                <CircleNotch className="size-4 animate-spin" aria-hidden />
              ) : (
                <ListChecks className="size-4" aria-hidden />
              )}
            </Button>
          )}
          <Button
            size="icon-sm"
            variant="ghost"
            onClick={toggleFullScreen}
            disabled={!workbench || closing}
            aria-label={fullScreenLabel}
          >
            {fullScreen ? (
              <ArrowsIn className="size-4" aria-hidden />
            ) : (
              <ArrowsOut className="size-4" aria-hidden />
            )}
          </Button>
        </div>
      </header>
      {showResults && (
        <div
          aria-live="polite"
          className={cn(
            "shrink-0 border-b border-border/60 transition-opacity",
            resultsStale && checkIssue === null && "opacity-60",
          )}
        >
          <CheckResults
            results={results}
            failed={checkIssue?.failed ?? false}
            emptyFile={checkIssue?.emptyFile ?? null}
            guided={guided}
            stale={resultsStale && checkIssue === null}
            onOpen={(path) => void workbench?.openFile(path)}
          />
        </div>
      )}
      {strip && <div className="shrink-0 border-b border-border/60 px-4 py-2">{strip}</div>}
      <div className="flex min-h-0 flex-1">
        {/* VS Code lays out left to right whatever the page direction. */}
        <div dir="ltr" className="relative min-w-0 flex-1">
          {/* Clipped, so nothing VS Code positions past its edge makes the
              boxes around it scrollable. */}
          <div ref={setHost} data-ide-host className="absolute inset-0 overflow-clip" />
          {!workbench && (
            <p className="absolute inset-0 grid place-items-center text-sm text-muted-foreground">
              {loadFailed
                ? v2Msg("submit.v2.ide.load_failed", guided)
                : v2Msg("submit.v2.ide.loading", guided)}
            </p>
          )}
        </div>
        {sidePanel && (
          <aside className="relative w-80 shrink-0 border-s border-border/60">
            {sidePanel}
          </aside>
        )}
      </div>
    </>
  );

  if (!fullScreen) {
    return (
      <section
        ref={slotRef}
        aria-label={v2Msg("submit.v2.metric.title", guided)}
        className="flex h-full min-h-0 flex-col"
      >
        {body}
      </section>
    );
  }
  // On body, not in place: the wizard's card has a backdrop filter, which
  // makes it the containing block of a fixed child, and the agent chat's own
  // tooltips and popovers portal to body at z-index 50, so the pop-up takes
  // the same layer and they open above it.
  // The empty slot stays in the step, so Exit full screen knows where to shrink to.
  return (
    <>
      <section ref={slotRef} aria-hidden className="h-full" />
      {createPortal(
        <div
          ref={layerRef}
          role="dialog"
          aria-modal="true"
          aria-label={v2Msg("submit.v2.metric.title", guided)}
          className="fixed inset-0 flex flex-col overflow-clip bg-background"
          style={{ zIndex: 50 }}
        >
          {body}
        </div>,
        document.body,
      )}
    </>
  );
}
