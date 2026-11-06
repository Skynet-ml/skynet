"use client";

import { PingDot } from "@/shared/ui/ping-dot";
import { motion } from "framer-motion";
import { GitBranch } from "@/shared/ui/icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { OptimizationStatusResponse } from "@/shared/types/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/primitives/card";
import { FadeIn } from "@/shared/ui/motion";
import { HelpTip } from "@/shared/ui/help-tip";
import { formatMsg, msg } from "@/shared/lib/messages";
import { TERMS } from "@/shared/lib/terms";
import {
  extractCandidates,
  extractMinibatch,
  extractRejected,
  scopeToLatestLane,
  extractValset,
  extractValsetOutputs,
} from "../lib/extract-events";
import { layoutTrajectory } from "../lib/layout";
import { rememberTrajectoryView } from "../lib/climb-view-hint";
import type { BlackboxTrajectoryContext } from "../lib/types";
import { TimelineScrubber } from "./TimelineScrubber";
import { TrajectoryTree } from "./TrajectoryTree";
import { TrajectoryDrawer, type DrawerSelection } from "./TrajectoryDrawer";

const NEWEST_HIGHLIGHT_MS = 2200;

// A black-box run rendered with nothing but the job in hand still gets
// black-box terminology — just no cases or renders to go on.
const BLACKBOX_FALLBACK: BlackboxTrajectoryContext = {
  hasCases: false,
  rendersByText: new Map(),
};

type Selected = { kind: "candidate" | "rejected"; id: string };

function generationText(gen: number): string {
  return formatMsg("trajectory.scrubber.generation_value", { gen });
}

function isLive(job: OptimizationStatusResponse): boolean {
  return job.status === "running" || job.status === "validating" || job.status === "pending";
}

export interface TrajectoryPanelProps {
  job: OptimizationStatusResponse;
  // When set, only candidate events tagged with this pair_index are kept —
  // grid-search pair views need to scope the tree to a single pair.
  pairIndex?: number;
  // Tool name → approval severity from the run's persisted react_overlay,
  // forwarded to the drawer so its tool cards match the Code tab.
  toolSeverities?: Record<string, string>;
  // Run configuration of a black-box run, forwarded to the drawer so it names
  // candidates by kind and shows per-case scores only when cases exist.
  blackbox?: BlackboxTrajectoryContext | null;
}

export function TrajectoryPanel({
  job,
  pairIndex,
  toolSeverities,
  blackbox,
}: TrajectoryPanelProps) {
  const live = isLive(job);
  const blackboxCtx = blackbox ?? (job.optimization_type === "blackbox" ? BLACKBOX_FALLBACK : null);
  const { candidates, rejected, valsetRows, minibatch, valsetOutputs } = useMemo(() => {
    const events = job.progress_events ?? [];
    const scoped = scopeToLatestLane(
      pairIndex === undefined ? events : events.filter((e) => e.metrics?.pair_index === pairIndex),
    );
    return {
      candidates: extractCandidates(scoped),
      rejected: extractRejected(scoped),
      valsetRows: extractValset(events),
      minibatch: extractMinibatch(scoped),
      valsetOutputs: extractValsetOutputs(scoped),
    };
  }, [job.progress_events, pairIndex]);
  const maxGeneration = useMemo(() => {
    let m = 0;
    for (const c of candidates) if (c.generation > m) m = c.generation;
    return m;
  }, [candidates]);
  const [generationFilter, setGenerationFilter] = useState<number | null>(null);
  const hintKey = pairIndex === undefined ? job.optimization_id : `${job.optimization_id}#pair`;
  const view = candidates.length === 0 ? "none" : maxGeneration > 0 ? "scrubber" : "plain";
  useEffect(() => {
    rememberTrajectoryView(hintKey, view);
  }, [hintKey, view]);
  const visibleCandidates = useMemo(() => {
    if (generationFilter === null) return candidates;
    return candidates.filter((c) => c.generation <= generationFilter);
  }, [candidates, generationFilter]);
  const visibleRejected = useMemo(() => {
    if (generationFilter === null) return rejected;
    const visibleIds = new Set(visibleCandidates.map((c) => c.candidate_id));
    return rejected.filter((r) => visibleIds.has(r.parent_id));
  }, [rejected, generationFilter, visibleCandidates]);
  const layout = useMemo(
    () => layoutTrajectory(visibleCandidates, visibleRejected),
    [visibleCandidates, visibleRejected],
  );
  const layoutWithoutRejected = useMemo(
    () => layoutTrajectory(visibleCandidates),
    [visibleCandidates],
  );
  const [selected, setSelected] = useState<Selected | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [newestId, setNewestId] = useState<string | null>(null);
  const newestTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const prevCountRef = useRef(0);
  const liveRegionRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (candidates.length === 0) {
      setSelected(null);
      return;
    }
    if (
      selected !== null &&
      selected.kind === "candidate" &&
      candidates.some((c) => c.candidate_id === selected.id)
    ) {
      return;
    }
    if (selected !== null && selected.kind === "rejected") {
      return;
    }
    if (layout.winnerId !== null) {
      setSelected({ kind: "candidate", id: layout.winnerId });
      return;
    }
    const first = candidates[0];
    if (first !== undefined) setSelected({ kind: "candidate", id: first.candidate_id });
  }, [candidates, layout.winnerId, selected]);

  useEffect(() => {
    if (candidates.length <= prevCountRef.current) {
      prevCountRef.current = candidates.length;
      return;
    }
    const newest = candidates[candidates.length - 1];
    prevCountRef.current = candidates.length;
    if (newest === undefined) return;
    setNewestId(newest.candidate_id);
    if (liveRegionRef.current !== null) {
      liveRegionRef.current.textContent = msg("trajectory.live.new_candidate");
    }
    if (newestTimerRef.current !== null) clearTimeout(newestTimerRef.current);
    newestTimerRef.current = setTimeout(() => setNewestId(null), NEWEST_HIGHLIGHT_MS);
    return () => {
      if (newestTimerRef.current !== null) clearTimeout(newestTimerRef.current);
    };
    // Tracking length only (a primitive) avoids re-running on every parent
    // render when ``candidates`` is reallocated by the upstream useMemo,
    // and keeps the deps shape stable across HMR-driven refreshes that
    // otherwise compared dep arrays of different lengths.
  }, [candidates.length]);

  const drawerSelection: DrawerSelection = useMemo(() => {
    if (selected === null) return null;
    if (selected.kind === "candidate") {
      const node = layout.nodes.find((n) => n.candidate_id === selected.id);
      if (node === undefined) return null;
      const parent =
        node.parent_id === null
          ? null
          : (layout.nodes.find((n) => n.candidate_id === node.parent_id) ?? null);
      return { kind: "candidate", node, parent };
    }
    const ghost = layout.ghosts.find((g) => g.rejection_id === selected.id);
    if (ghost === undefined) return null;
    const parent = layout.nodes.find((n) => n.candidate_id === ghost.parent_id) ?? null;
    return { kind: "rejected", ghost, parent };
  }, [layout.nodes, layout.ghosts, selected]);

  const selectedTreeId = selected !== null && selected.kind === "candidate" ? selected.id : null;

  const handleSelectCandidate = useCallback((id: string) => {
    setSelected({ kind: "candidate", id });
    setDrawerOpen(true);
  }, []);

  const handleSelectRejected = useCallback((id: string) => {
    setSelected({ kind: "rejected", id });
    setDrawerOpen(true);
  }, []);

  if (candidates.length === 0) return null;

  return (
    <FadeIn delay={0.12}>
      <Card className="relative overflow-hidden">
        <div
          className="absolute inset-x-0 top-0 h-px bg-gradient-to-l from-transparent via-primary-fill/40 to-transparent"
          aria-hidden="true"
        />
        <CardHeader className="flex flex-row items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <CardTitle className="text-base flex min-w-0 items-center gap-2">
              <GitBranch className="size-4" aria-hidden="true" />
              <HelpTip text={msg("trajectory.explainer.trajectory")}>
                {msg("trajectory.panel.title")}
              </HelpTip>
            </CardTitle>
          </div>
          {live ? (
            <motion.div
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              className="flex shrink-0 items-center gap-1.5 rounded-md border border-border/50 bg-background/80 px-3 py-1 text-[0.6875rem] font-semibold uppercase tracking-wider rtl:tracking-normal text-muted-foreground/80 backdrop-blur"
            >
              <PingDot size="sm" />
              <span className="tabular-nums">{candidates.length}</span>
              <span>{TERMS.candidatePlural}</span>
            </motion.div>
          ) : (
            <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider rtl:tracking-normal text-muted-foreground">
              <span className="tabular-nums">{candidates.length}</span> {TERMS.candidatePlural}
            </span>
          )}
        </CardHeader>
        <CardContent className="space-y-3">
          {maxGeneration > 0 ? (
            <TimelineScrubber
              max={maxGeneration}
              value={generationFilter}
              onChange={setGenerationFilter}
              isLive={live}
              label={msg("trajectory.scrubber.label")}
              stepText={generationText}
              liveText={msg("trajectory.scrubber.live")}
            />
          ) : null}
          <TrajectoryTree
            layout={layout}
            layoutWithoutRejected={layoutWithoutRejected}
            selectedId={selectedTreeId}
            newestId={newestId}
            onSelectCandidate={handleSelectCandidate}
            onSelectRejected={handleSelectRejected}
            ringMode={blackboxCtx === null ? "pass_fail" : "score"}
          />
          <TrajectoryDrawer
            selection={drawerSelection}
            open={drawerOpen}
            onOpenChange={setDrawerOpen}
            valsetRows={valsetRows}
            minibatch={minibatch}
            blackbox={blackboxCtx}
            valsetOutputs={valsetOutputs}
            toolSeverities={toolSeverities}
          />
          <div ref={liveRegionRef} role="status" aria-live="polite" className="sr-only" />
        </CardContent>
      </Card>
    </FadeIn>
  );
}
