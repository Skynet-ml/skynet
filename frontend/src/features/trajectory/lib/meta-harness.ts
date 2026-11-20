import type { ProgressEvent } from "@/shared/types/api";
import type { CandidateMetrics } from "./types";

const CANDIDATE_EVENT = "candidate";
const REJECTED_EVENT = "candidate_rejected";
const MINIBATCH_EVENT = "minibatch_feedback";
const CASE_SCORED_EVENT = "case_scored";
const LANE_STARTED_EVENT = "lane_started";
const AGENT_RUN_EVENT = "agent_run";

const AGENT_RUN_PHASE_VERSION = "version";
export const AGENT_RUN_PHASE_BASELINE = "baseline";
export const AGENT_RUN_PHASE_FINAL = "final";
export const AGENT_RUN_STATUS_RUNNING = "running";
export const AGENT_RUN_STATUS_FAILED = "failed";

const LANED_EVENTS = new Set([CANDIDATE_EVENT, REJECTED_EVENT, MINIBATCH_EVENT, CASE_SCORED_EVENT]);

/** Engines that hill-climb: every version is scored on every case, so the run view draws a climb. */
const CLIMB_ENGINES: ReadonlySet<string> = new Set([
  "meta_harness",
  "autoresearch",
  "autosaddler",
  "shinka_evolve",
]);

function laneOf(event: ProgressEvent): number {
  const lane = event.metrics?.lane_index;
  return typeof lane === "number" && Number.isInteger(lane) && lane > 0 ? lane : 0;
}

/**
 * Index of the lane the newest version events belong to.
 *
 * A version's case scores stream in before its candidate event, so both count:
 * otherwise the first version of a new lane would be read as part of the
 * previous lane until it completes.
 */
export function latestVersionLane(events: ProgressEvent[]): number {
  let latest = 0;
  for (const event of events) {
    if (event.event === CANDIDATE_EVENT || event.event === CASE_SCORED_EVENT) {
      latest = Math.max(latest, laneOf(event));
    }
  }
  return latest;
}

// The held-out agent runs (starting point, best version) belong to the run as
// a whole; only the per-version ones are bound to a lane.
function isLaneBound(event: ProgressEvent): boolean {
  if (event.event == null) return false;
  if (event.event === AGENT_RUN_EVENT) return event.metrics?.phase === AGENT_RUN_PHASE_VERSION;
  return LANED_EVENTS.has(event.event);
}

/**
 * Keep only the version events of the newest lane — ``scopeToLatestLane`` with
 * per-case scores and per-version agent runs counted as version events too.
 */
export function scopeToVersionLane(events: ProgressEvent[]): ProgressEvent[] {
  const latest = latestVersionLane(events);
  if (latest === 0) return events;
  return events.filter((event) => !isLaneBound(event) || laneOf(event) === latest);
}

/**
 * Engine of the lane the newest versions belong to.
 *
 * ``lane_started`` events carry the engine but no lane index; every strategy
 * mode starts its lanes in order, so the N-th one announces lane N.
 */
export function engineOfLatestLane(events: ProgressEvent[]): string | null {
  const lane = latestVersionLane(events);
  let seen = 0;
  for (const event of events) {
    if (event.event !== LANE_STARTED_EVENT) continue;
    if (seen === lane) {
      const engine = event.metrics?.engine;
      return typeof engine === "string" ? engine : null;
    }
    seen += 1;
  }
  return null;
}

/**
 * The climbing engine whose versions the run view should draw, or null for a
 * branching run that gets the tree.
 *
 * The lane that produced the newest versions decides while events are in hand;
 * a run without lane events falls back to the engine the result names, then to
 * the one the strategy asked for.
 */
export function climbEngineOf(
  events: ProgressEvent[],
  engineUsed: string | null | undefined,
  strategyEngine: string | null | undefined,
): string | null {
  const engine = engineOfLatestLane(events) ?? engineUsed ?? strategyEngine ?? null;
  return engine !== null && CLIMB_ENGINES.has(engine) ? engine : null;
}

export interface CaseScore {
  trial: number;
  example_id: string;
  score: number;
  total: number;
}

export function extractCaseScores(events: ProgressEvent[]): CaseScore[] {
  const out: CaseScore[] = [];
  for (const event of events) {
    if (event.event !== CASE_SCORED_EVENT) continue;
    const m = event.metrics ?? {};
    if (typeof m.trial !== "number" || typeof m.score !== "number") continue;
    const exampleId =
      typeof m.example_id === "string"
        ? m.example_id
        : typeof m.example_id === "number"
          ? String(m.example_id)
          : null;
    if (exampleId === null) continue;
    out.push({
      trial: m.trial,
      example_id: exampleId,
      score: m.score,
      total: typeof m.total === "number" ? m.total : 0,
    });
  }
  return out;
}

// What the ``agent_run`` progress event carries: enough to place the run in
// the grid and show whether it is still going; the record itself is fetched
// on demand.
export interface AgentRunSummary {
  run_id: number;
  phase: string;
  trial: number | null;
  example_id: string | null;
  case_id: string | null;
  label: string;
  status: string;
  exit_code: number | null;
  timed_out: boolean;
  error: string | null;
  elapsed_seconds: number | null;
}

function idOf(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return null;
}

/** The latest summary of every agent run, in the order the runs started. */
export function extractAgentRuns(events: ProgressEvent[]): AgentRunSummary[] {
  const byId = new Map<number, AgentRunSummary>();
  for (const event of events) {
    if (event.event !== AGENT_RUN_EVENT) continue;
    const m = event.metrics ?? {};
    if (typeof m.run_id !== "number" || typeof m.phase !== "string") continue;
    if (typeof m.status !== "string") continue;
    byId.set(m.run_id, {
      run_id: m.run_id,
      phase: m.phase,
      trial: typeof m.trial === "number" ? m.trial : null,
      example_id: idOf(m.example_id),
      case_id: typeof m.case_id === "string" ? m.case_id : null,
      label: typeof m.label === "string" ? m.label : "",
      status: m.status,
      exit_code: typeof m.exit_code === "number" ? m.exit_code : null,
      timed_out: m.timed_out === true,
      error: typeof m.error === "string" ? m.error : null,
      elapsed_seconds: typeof m.elapsed_seconds === "number" ? m.elapsed_seconds : null,
    });
  }
  return [...byId.values()];
}

/** Grid address of a per-version run: the version index and the case it ran. */
export function agentRunKey(trial: number, exampleId: string): string {
  return `${trial}:${exampleId}`;
}

export function finalRunKey(exampleId: string): string {
  return `final:${exampleId}`;
}

/**
 * Runs by the version and case they scored, so a case can open the run behind
 * its score. The starting point is scored before any proposal, so its runs
 * file under version 0; the final check of the best version keeps its own key.
 */
export function indexAgentRuns(runs: AgentRunSummary[]): Map<string, AgentRunSummary> {
  const byCell = new Map<string, AgentRunSummary>();
  for (const run of runs) {
    if (run.example_id === null) continue;
    if (run.phase === AGENT_RUN_PHASE_FINAL) {
      byCell.set(finalRunKey(run.example_id), run);
      continue;
    }
    const trial = run.phase === AGENT_RUN_PHASE_BASELINE ? 0 : run.trial;
    if (trial === null) continue;
    byCell.set(agentRunKey(trial, run.example_id), run);
  }
  return byCell;
}

interface ClimbVersion {
  candidate: CandidateMetrics;
  index: number;
  score: number;
  // Best score among the versions before this one; null for the first.
  bestBefore: number | null;
  improved: boolean;
}

// A version still being scored: its cases fill in one by one until the
// candidate event completes it.
interface PendingVersion {
  index: number;
  total: number;
  scores: Map<string, number>;
}

// One case of the pending version: scored, or still in the box.
export interface PendingCase {
  id: string;
  score: number | null;
}

export interface ClimbModel {
  versions: ClimbVersion[];
  caseIds: string[];
  pending: PendingVersion | null;
  bestId: string | null;
}

function compareIds(a: string, b: string): number {
  const na = Number(a);
  const nb = Number(b);
  if (Number.isFinite(na) && Number.isFinite(nb)) return na - nb;
  return a.localeCompare(b);
}

/**
 * Turn the lane's versions into a climb: each version measured against the
 * best before it, plus the version currently being scored, if any. That
 * version's sandbox runs start before its first case is scored, so a
 * version-phase run on a later trial than any finished version announces
 * it too.
 */
export function buildClimb(
  candidates: CandidateMetrics[],
  caseScores: CaseScore[],
  runs: AgentRunSummary[] = [],
  // SKY-750: a minimize run improves when its score goes down.
  minimize = false,
): ClimbModel {
  const sorted = [...candidates].sort((a, b) => compareIds(a.candidate_id, b.candidate_id));
  const versions: ClimbVersion[] = [];
  const caseIds = new Set<string>();
  let best: number | null = null;
  let bestId: string | null = null;
  let lastIndex = -1;
  for (const candidate of sorted) {
    const parsed = Number(candidate.candidate_id);
    const index = Number.isFinite(parsed) ? parsed : versions.length;
    const improved = best === null || (minimize ? candidate.score < best : candidate.score > best);
    versions.push({ candidate, index, score: candidate.score, bestBefore: best, improved });
    if (improved) {
      best = candidate.score;
      bestId = candidate.candidate_id;
    }
    lastIndex = Math.max(lastIndex, index);
    for (const entry of candidate.per_example) caseIds.add(entry.id);
  }

  const versionRuns = runs.filter(
    (run) => run.phase === AGENT_RUN_PHASE_VERSION && run.trial !== null,
  );
  let pendingIndex = -1;
  for (const scored of caseScores) {
    if (scored.trial > lastIndex) pendingIndex = Math.max(pendingIndex, scored.trial);
  }
  for (const run of versionRuns) {
    if (run.trial !== null && run.trial > lastIndex)
      pendingIndex = Math.max(pendingIndex, run.trial);
  }
  let pending: PendingVersion | null = null;
  if (pendingIndex >= 0) {
    pending = { index: pendingIndex, total: 0, scores: new Map() };
    for (const scored of caseScores) {
      if (scored.trial !== pendingIndex) continue;
      pending.scores.set(scored.example_id, scored.score);
      pending.total = Math.max(pending.total, scored.total);
      caseIds.add(scored.example_id);
    }
    for (const run of versionRuns) {
      if (run.trial === pendingIndex && run.example_id !== null) caseIds.add(run.example_id);
    }
    // The case count arrives with the first score; until then every case
    // seen so far stands in for it.
    pending.total = Math.max(pending.total, caseIds.size);
  }

  return { versions, caseIds: [...caseIds].sort(compareIds), pending, bestId };
}

/**
 * List every case of the pending version with its score so far.
 *
 * Args:
 *   model: The climb the version belongs to.
 *
 * Returns:
 *   One entry per case the job scores, unscored ones with a ``null`` score;
 *   empty when no version is being scored.
 */
export function pendingCases(model: ClimbModel): PendingCase[] {
  const pending = model.pending;
  if (pending === null) return [];
  return model.caseIds.map((id) => ({ id, score: pending.scores.get(id) ?? null }));
}

export const CLIMB_LAYOUT = {
  nodeRadius: 22,
  ringThickness: 6,
  stepMin: 88,
  stepMax: 168,
  padStart: 64,
  padEnd: 44,
  padTop: 40,
  padBottom: 44,
  plotHeight: 280,
  tickCount: 5,
} as const;

export interface ClimbPoint {
  id: string;
  x: number;
  y: number;
  version: ClimbVersion;
}

export interface ClimbLayout {
  points: ClimbPoint[];
  pending: { x: number; y: number } | null;
  edges: Array<{ from: ClimbPoint; to: ClimbPoint }>;
  ticks: Array<{ value: number; y: number }>;
  domain: { min: number; max: number; unit: boolean };
  width: number;
  height: number;
}

export function meanOf(values: Iterable<number>): number | null {
  let sum = 0;
  let count = 0;
  for (const value of values) {
    sum += value;
    count += 1;
  }
  return count === 0 ? null : sum / count;
}

function scoreDomain(scores: number[]): ClimbLayout["domain"] {
  if (scores.length === 0) return { min: 0, max: 1, unit: true };
  const unit = scores.every((s) => s >= 0 && s <= 1);
  if (unit) return { min: 0, max: 1, unit: true };
  let min = Math.min(...scores);
  let max = Math.max(...scores);
  const pad = max > min ? (max - min) * 0.1 : Math.max(1, Math.abs(max) * 0.1);
  min -= pad;
  max += pad;
  return { min, max, unit: false };
}

/**
 * Place the climb on a version-by-score plane.
 *
 * Versions sit left to right in the order they were scored; the column pitch
 * stretches to fill the width on hand between ``stepMin`` and ``stepMax``,
 * and the plot grows past ``plotHeight`` to fill the height on hand.
 */
export function layoutClimb(
  model: ClimbModel,
  options: { availableWidth?: number; availableHeight?: number; showPending?: boolean } = {},
): ClimbLayout {
  const L = CLIMB_LAYOUT;
  const plotHeight = Math.max(
    L.plotHeight,
    (options.availableHeight ?? 0) - L.padTop - L.padBottom,
  );
  const pendingMean = model.pending === null ? null : meanOf(model.pending.scores.values());
  const showPending = options.showPending === true && model.pending !== null;
  const scores = model.versions.map((v) => v.score);
  if (showPending && pendingMean !== null) scores.push(pendingMean);
  const domain = scoreDomain(scores);
  const span = domain.max - domain.min;
  const yOf = (score: number): number =>
    L.padTop + plotHeight - ((score - domain.min) / span) * plotHeight;

  const columns = model.versions.length + (showPending ? 1 : 0);
  const available = options.availableWidth ?? 0;
  const step =
    columns <= 1
      ? L.stepMin
      : Math.max(
          L.stepMin,
          Math.min(L.stepMax, (available - L.padStart - L.padEnd) / (columns - 1)),
        );
  const xOf = (column: number): number => L.padStart + column * step;

  const points: ClimbPoint[] = model.versions.map((version, column) => ({
    id: version.candidate.candidate_id,
    x: xOf(column),
    y: yOf(version.score),
    version,
  }));
  const byId = new Map(points.map((p) => [p.id, p]));

  // The best version follows the run's direction, which only the model knows.
  const best = model.bestId !== null ? (byId.get(model.bestId)?.version.score ?? null) : null;

  const edges: ClimbLayout["edges"] = [];
  for (const point of points) {
    const parentId = point.version.candidate.parent_id;
    if (parentId === null) continue;
    const parent = byId.get(parentId);
    if (parent !== undefined) edges.push({ from: parent, to: point });
  }

  // Until a case is scored the pending version rides at the score it is
  // rewriting, then settles on the mean of the cases scored so far.
  let pending: ClimbLayout["pending"] = null;
  if (showPending) {
    const y =
      pendingMean !== null
        ? yOf(pendingMean)
        : best !== null
          ? yOf(best)
          : yOf((domain.min + domain.max) / 2);
    pending = { x: xOf(columns - 1), y };
  }

  const ticks: ClimbLayout["ticks"] = [];
  for (let i = 0; i < L.tickCount; i += 1) {
    const value = domain.min + (span * i) / (L.tickCount - 1);
    ticks.push({ value, y: yOf(value) });
  }

  return {
    points,
    pending,
    edges,
    ticks,
    domain,
    width: L.padStart + Math.max(0, columns - 1) * step + L.padEnd,
    height: L.padTop + plotHeight + L.padBottom,
  };
}
