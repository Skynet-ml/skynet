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
import { PingDot } from "@/shared/ui/ping-dot";
import { Button } from "@/shared/ui/primitives/button";
import { ArrowsIn, ArrowsOut } from "@/shared/ui/icons";
import { diffWorkspace, workspaceTexts } from "../lib/ide-workspace";
import { v2Msg } from "../components/v2/ArtifactConfirmCard";
import { park, unpark } from "./park";
import type { RepoSource } from "./repo-files";
import { guardClippingAncestors, guardClippingInside } from "./scroll-guard";
import type { IdeWorkbench } from "./workbench";
import { ZOOM_EASING, ZOOM_MS, prefersReducedMotion, zoomFrames } from "./zoom";

function isDark(): boolean {
  return document.documentElement.classList.contains("dark");
}

/**
 * The Metric step's workspace as VS Code's workbench: explorer, tabs, search
 * and the command palette over the wizard's own files, with the setup agent
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
  repo,
  lockedFiles,
  strip,
  sidePanel,
  guided,
  openRequest,
}: {
  runCode: string;
  metricCode: string;
  files: Readonly<Record<string, string>>;
  onFileChange: (path: string, text: string) => void;
  onFileDelete: (path: string) => void;
  /** A repository run's checkout, shown read-only in the explorer. */
  repo: RepoSource | null;
  /** Workspace files the setup agent may write now; the editor holds them read-only. */
  lockedFiles?: readonly string[];
  /** Shown above the editor (the metric model row). */
  strip?: ReactNode;
  /** The end-side region: the setup agent's chat. */
  sidePanel?: ReactNode;
  guided: boolean;
  /** Open this file, at this line, once the workbench is up; a new object opens it again. */
  openRequest?: { path: string; line?: number } | null;
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

  useEffect(() => () => workbench?.setRepo(null), [workbench]);

  useEffect(() => {
    if (workbench && openRequest) void workbench.openFile(openRequest.path, openRequest.line);
  }, [workbench, openRequest]);

  const lockedKey = (lockedFiles ?? []).join("\n");
  useEffect(() => {
    workbench?.setLocked(lockedKey ? lockedKey.split("\n") : []);
  }, [workbench, lockedKey]);

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

  const fullScreenLabel = fullScreen
    ? v2Msg("submit.v2.ide.exit_full_screen", guided)
    : v2Msg("submit.v2.ide.full_screen", guided);

  const body = (
    <>
      <header className="flex shrink-0 items-center gap-3 border-b border-border/60 px-4 py-2">
        {lockedKey && (
          <span role="status" className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
            <PingDot size="sm" tone="agent" />
            <span className="truncate">{v2Msg("submit.v2.ide.agent_writing", guided)}</span>
          </span>
        )}
        <div className="ms-auto flex shrink-0 items-center gap-1">
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
