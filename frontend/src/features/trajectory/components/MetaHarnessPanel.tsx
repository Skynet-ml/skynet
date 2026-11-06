"use client";

import { PingDot } from "@/shared/ui/ping-dot";
import { motion } from "framer-motion";
import { TrendUp } from "@/shared/ui/icons";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { OptimizationStatusResponse } from "@/shared/types/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/primitives/card";
import { FadeIn } from "@/shared/ui/motion";
import { HelpTip } from "@/shared/ui/help-tip";
import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import {
  extractCandidates,
  extractMinibatch,
  extractValset,
  extractValsetOutputs,
} from "../lib/extract-events";
import { layoutTrajectory } from "../lib/layout";
import {
  agentRunKey,
  buildClimb,
  extractAgentRuns,
  extractCaseScores,
  finalRunKey,
  indexAgentRuns,
  pendingCases,
  scopeToVersionLane,
  type AgentRunSummary,
} from "../lib/meta-harness";
import { displayCandidateId, type BlackboxTrajectoryContext } from "../lib/types";
import { rememberTrajectoryView } from "../lib/climb-view-hint";
import type { ApiSource } from "@/shared/lib/api-route";
import { AgentRunViewer } from "./AgentRunViewer";
import { MetaHarnessClimb } from "./MetaHarnessClimb";
import { TimelineScrubber } from "./TimelineScrubber";
import { TrajectoryDrawer, type DrawerSelection } from "./TrajectoryDrawer";

const NEWEST_HIGHLIGHT_MS = 2200;

const BLACKBOX_FALLBACK: BlackboxTrajectoryContext = {
  hasCases: false,
  rendersByText: new Map(),
};

function versionText(index: number): string {
  return formatMsg("meta_harness.version", { id: displayCandidateId(String(index)) });
}

function versionTick(index: number): string {
  return displayCandidateId(String(index));
}

function isLive(job: OptimizationStatusResponse): boolean {
  return job.status === "running" || job.status === "validating" || job.status === "pending";
}

// Each climbing engine explains its own loop; Meta-Harness keeps the default.
const ENGINE_EXPLAINERS: Record<string, MessageKey> = {
  autoresearch: "meta_harness.explainer.autoresearch",
  autosaddler: "meta_harness.explainer.autosaddler",
  shinka_evolve: "meta_harness.explainer.shinka_evolve",
};

export interface MetaHarnessPanelProps {
  job: OptimizationStatusResponse;
  // The hill-climbing engine that produced the versions: meta_harness,
  // autoresearch, autosaddler or shinka_evolve.
  engine: string;
  // Run configuration of the black-box run, forwarded to the drawer so it
  // names versions by kind and shows per-case scores only when cases exist.
  blackbox?: BlackboxTrajectoryContext | null;
  // Where the run lives; the agent-run drawer reads from the same backend.
  source?: ApiSource;
}

/**
 * Run view of a hill-climbing lane (Meta-Harness, AutoResearch, AutoSaddler,
 * ShinkaEvolve).
 * The engine rewrites the best version so far and scores every candidate on
 * all cases, so the run is a climb rather than a tree: the chart lays versions out in scoring order
 * with the best so far as a staircase, and a version's drawer shows how it did
 * case by case, each score opening the agent run behind it.
 */
export function MetaHarnessPanel({ job, engine, blackbox, source }: MetaHarnessPanelProps) {
  const live = isLive(job);
  const optimizationId = job.optimization_id;

  const blackboxCtx = blackbox ?? BLACKBOX_FALLBACK;
  const { candidates, caseScores, valsetRows, minibatch, valsetOutputs, agentRuns } =
    useMemo(() => {
      const events = job.progress_events ?? [];
      const scoped = scopeToVersionLane(events);
      return {
        candidates: extractCandidates(scoped),
        caseScores: extractCaseScores(scoped),
        valsetRows: extractValset(events),
        minibatch: extractMinibatch(scoped),
        valsetOutputs: extractValsetOutputs(scoped),
        agentRuns: extractAgentRuns(scoped),
      };
    }, [job.progress_events]);
  const runsByCell = useMemo(() => indexAgentRuns(agentRuns), [agentRuns]);
  const fullModel = useMemo(
    () => buildClimb(candidates, caseScores, agentRuns),
    [candidates, caseScores, agentRuns],
  );
  const maxVersion = useMemo(
    () => fullModel.versions.reduce((last, version) => Math.max(last, version.index), 0),
    [fullModel],
  );
  const [versionFilter, setVersionFilter] = useState<number | null>(null);
  // Scrubbed back, the climb ends at that version: later versions and the one
  // still being scored drop out until the knob returns to the live end.
  const model = useMemo(() => {
    if (versionFilter === null) return fullModel;
    const kept = new Set(
      fullModel.versions
        .filter((version) => version.index <= versionFilter)
        .map((version) => version.candidate.candidate_id),
    );
    return buildClimb(
      candidates.filter((candidate) => kept.has(candidate.candidate_id)),
      caseScores.filter((scored) => scored.trial <= versionFilter),
    );
  }, [fullModel, candidates, caseScores, versionFilter]);
  const treeLayout = useMemo(() => layoutTrajectory(candidates), [candidates]);
  // The version being scored answers to its trial number, which is also the
  // candidate id it gets once complete, so a selection on it carries over.
  const pendingId = live && fullModel.pending !== null ? String(fullModel.pending.index) : null;

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [openRun, setOpenRun] = useState<AgentRunSummary | null>(null);
  const [runOpen, setRunOpen] = useState(false);
  const [newestId, setNewestId] = useState<string | null>(null);
  const newestTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const prevCountRef = useRef(0);
  const liveRegionRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (candidates.length === 0 && pendingId === null) {
      setSelectedId(null);
      return;
    }
    if (
      selectedId !== null &&
      (selectedId === pendingId || candidates.some((c) => c.candidate_id === selectedId))
    ) {
      return;
    }
    setSelectedId(fullModel.bestId ?? candidates[0]?.candidate_id ?? null);
  }, [candidates, fullModel.bestId, selectedId, pendingId]);

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
      liveRegionRef.current.textContent = msg("meta_harness.live.new_version");
    }
    if (newestTimerRef.current !== null) clearTimeout(newestTimerRef.current);
    newestTimerRef.current = setTimeout(() => setNewestId(null), NEWEST_HIGHLIGHT_MS);
    return () => {
      if (newestTimerRef.current !== null) clearTimeout(newestTimerRef.current);
    };
    // Tracking the length only keeps this from re-running each time the
    // upstream useMemo reallocates ``candidates`` on an unrelated render.
  }, [candidates.length]);

  const drawerSelection: DrawerSelection = useMemo(() => {
    if (selectedId === null) return null;
    if (selectedId === pendingId && fullModel.pending !== null) {
      return {
        kind: "pending",
        index: fullModel.pending.index,
        total: fullModel.pending.total,
        cases: pendingCases(fullModel),
      };
    }
    const node = treeLayout.nodes.find((n) => n.candidate_id === selectedId);
    if (node === undefined) return null;
    const parent =
      node.parent_id === null
        ? null
        : (treeLayout.nodes.find((n) => n.candidate_id === node.parent_id) ?? null);
    return { kind: "candidate", node, parent };
  }, [treeLayout.nodes, selectedId, pendingId, fullModel]);

  const handleSelect = useCallback((id: string) => {
    setSelectedId(id);
    setDrawerOpen(true);
  }, []);

  const handleOpenRun = useCallback((run: AgentRunSummary) => {
    setOpenRun(run);
    setRunOpen(true);
  }, []);

  // The runs behind the selected version's case scores: its own, plus the
  // final check of the best version once the engine has stopped. The version
  // still being scored has only its own, some of them still going.
  const caseRuns = useCallback(
    (exampleId: string): AgentRunSummary[] => {
      if (selectedId === pendingId && fullModel.pending !== null) {
        const own = runsByCell.get(agentRunKey(fullModel.pending.index, exampleId));
        return own === undefined ? [] : [own];
      }
      const version = fullModel.versions.find((v) => v.candidate.candidate_id === selectedId);
      if (version === undefined) return [];
      const runs: AgentRunSummary[] = [];
      const own = runsByCell.get(agentRunKey(version.index, exampleId));
      if (own !== undefined) runs.push(own);
      const final =
        selectedId === fullModel.bestId ? runsByCell.get(finalRunKey(exampleId)) : undefined;
      if (final !== undefined) runs.push(final);
      return runs;
    },
    [fullModel, runsByCell, selectedId, pendingId],
  );

  const hidden = fullModel.versions.length === 0 && (!live || fullModel.pending === null);
  useEffect(() => {
    rememberTrajectoryView(optimizationId, hidden ? "none" : "climb");
  }, [optimizationId, hidden]);

  if (hidden) return null;

  const versionCount = formatMsg("meta_harness.header.versions", {
    n: fullModel.versions.length,
  });

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
              <TrendUp className="size-4" aria-hidden="true" />
              <HelpTip text={msg(ENGINE_EXPLAINERS[engine] ?? "meta_harness.explainer")}>
                {msg("meta_harness.panel.title")}
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
              <span className="tabular-nums">{versionCount}</span>
            </motion.div>
          ) : (
            <span className="shrink-0 text-[10px] font-semibold uppercase tracking-wider rtl:tracking-normal text-muted-foreground tabular-nums">
              {versionCount}
            </span>
          )}
        </CardHeader>
        <CardContent className="space-y-4">
          {maxVersion > 0 ? (
            <TimelineScrubber
              max={maxVersion}
              value={versionFilter}
              onChange={setVersionFilter}
              isLive={live}
              label={msg("meta_harness.scrubber.label")}
              stepText={versionText}
              liveText={msg("trajectory.scrubber.live")}
              tickText={versionTick}
            />
          ) : null}
          <MetaHarnessClimb
            model={model}
            live={live}
            selectedId={selectedId}
            newestId={newestId}
            onSelect={handleSelect}
          />
          <AgentRunViewer
            optimizationId={job.optimization_id}
            run={openRun}
            open={runOpen}
            onOpenChange={setRunOpen}
            source={source}
          />
          <TrajectoryDrawer
            selection={drawerSelection}
            open={drawerOpen}
            onOpenChange={setDrawerOpen}
            valsetRows={valsetRows}
            minibatch={minibatch}
            blackbox={blackboxCtx}
            valsetOutputs={valsetOutputs}
            caseRuns={caseRuns}
            onOpenRun={handleOpenRun}
          />
          <div ref={liveRegionRef} role="status" aria-live="polite" className="sr-only" />
        </CardContent>
      </Card>
    </FadeIn>
  );
}
