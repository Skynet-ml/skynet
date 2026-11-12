import type { SideImage } from "@/shared/lib/candidate-render";
import type { ProgressEvent } from "@/shared/types/api";
import type {
  CandidateMetrics,
  MinibatchEntry,
  PerExampleScore,
  RejectedMetrics,
} from "./types";

const CANDIDATE_EVENT = "candidate";
const REJECTED_EVENT = "candidate_rejected";
const MINIBATCH_EVENT = "minibatch_feedback";

/**
 * Keep only the trajectory events of the newest engine lane.
 *
 * Black-box strategies run engines in sequential lanes and candidate ids
 * restart at "0" in each one, so mixing lanes would corrupt the tree. Events
 * without a ``lane_index`` (single-engine runs) are lane 0, making this a
 * no-op for them.
 */
export function scopeToLatestLane(events: ProgressEvent[]): ProgressEvent[] {
  const laneOf = (event: ProgressEvent): number => {
    const lane = event.metrics?.lane_index;
    return typeof lane === "number" && Number.isInteger(lane) && lane > 0 ? lane : 0;
  };
  let latest = 0;
  for (const event of events) {
    if (event.event === CANDIDATE_EVENT) latest = Math.max(latest, laneOf(event));
  }
  if (latest === 0) return events;
  const laned = new Set([CANDIDATE_EVENT, REJECTED_EVENT, MINIBATCH_EVENT]);
  return events.filter(
    (event) => event.event == null || !laned.has(event.event) || laneOf(event) === latest,
  );
}

function coercePerExample(raw: unknown): PerExampleScore[] {
  if (!Array.isArray(raw)) return [];
  const out: PerExampleScore[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const e = entry as Record<string, unknown>;
    if (typeof e.id !== "string") continue;
    if (typeof e.score !== "number") continue;
    out.push({ id: e.id, score: e.score });
  }
  return out;
}

function coercePrompt(raw: unknown): Record<string, string> {
  if (!raw || typeof raw !== "object") return {};
  return Object.fromEntries(
    Object.entries(raw as Record<string, unknown>).filter(
      (entry): entry is [string, string] => typeof entry[1] === "string",
    ),
  );
}

function coerceCandidate(
  metrics: Record<string, unknown>,
  timestamp: string,
): CandidateMetrics | null {
  const candidate_id = metrics.candidate_id;
  const generation = metrics.generation;
  const score = metrics.score;
  if (typeof candidate_id !== "string") return null;
  if (typeof generation !== "number") return null;
  if (typeof score !== "number") return null;

  const parent_id = metrics.parent_id;
  const parents_extra_raw = metrics.parents_extra;
  const discovered_at_evals = metrics.discovered_at_evals;
  const iteration = metrics.iteration;

  return {
    candidate_id,
    parent_id: typeof parent_id === "string" ? parent_id : null,
    parents_extra: Array.isArray(parents_extra_raw)
      ? parents_extra_raw.filter((v): v is string => typeof v === "string")
      : [],
    generation,
    score,
    per_example: coercePerExample(metrics.per_example),
    prompt: coercePrompt(metrics.prompt),
    discovered_at_evals: typeof discovered_at_evals === "number" ? discovered_at_evals : 0,
    iteration: typeof iteration === "number" ? iteration : null,
    timestamp,
  };
}

export function extractCandidates(events: ProgressEvent[]): CandidateMetrics[] {
  const out: CandidateMetrics[] = [];
  const seen = new Set<string>();
  for (const event of events) {
    if (event.event !== CANDIDATE_EVENT) continue;
    const parsed = coerceCandidate(event.metrics ?? {}, event.timestamp);
    if (parsed === null) continue;
    if (seen.has(parsed.candidate_id)) continue;
    seen.add(parsed.candidate_id);
    out.push(parsed);
  }
  return out.sort((a, b) => Number(a.candidate_id) - Number(b.candidate_id));
}

function coerceRejected(metrics: Record<string, unknown>): RejectedMetrics | null {
  const rejection_id = metrics.rejection_id;
  const iteration = metrics.iteration;
  const parent_id = metrics.parent_id;
  const parent_score = metrics.parent_score;
  const proposal_score = metrics.proposal_score;
  const subsample_size = metrics.subsample_size;
  if (typeof rejection_id !== "string") return null;
  if (typeof iteration !== "number") return null;
  if (typeof parent_id !== "string") return null;
  if (typeof parent_score !== "number") return null;
  if (typeof proposal_score !== "number") return null;
  if (typeof subsample_size !== "number") return null;
  const subsample_ids_raw = metrics.subsample_ids;
  return {
    rejection_id,
    iteration,
    parent_id,
    parent_score,
    proposal_score,
    subsample_size,
    proposal_prompt: coercePrompt(metrics.proposal_prompt),
    parent_prompt: coercePrompt(metrics.parent_prompt),
    subsample_ids: Array.isArray(subsample_ids_raw)
      ? subsample_ids_raw.filter((v): v is string => typeof v === "string")
      : [],
    per_example_parent: coercePerExample(metrics.per_example_parent),
    per_example_proposal: coercePerExample(metrics.per_example_proposal),
  };
}

export function extractRejected(events: ProgressEvent[]): RejectedMetrics[] {
  const out: RejectedMetrics[] = [];
  const seen = new Set<string>();
  for (const event of events) {
    if (event.event !== REJECTED_EVENT) continue;
    const parsed = coerceRejected(event.metrics ?? {});
    if (parsed === null) continue;
    if (seen.has(parsed.rejection_id)) continue;
    seen.add(parsed.rejection_id);
    out.push(parsed);
  }
  return out.sort((a, b) => a.iteration - b.iteration);
}

function coerceMinibatch(
  metrics: Record<string, unknown>,
  sequence: number,
): MinibatchEntry | null {
  const example_id = metrics.example_id;
  const score = metrics.score;
  const feedback = metrics.feedback;
  const iteration = metrics.iteration;
  const images_dropped = metrics.images_dropped;
  if (typeof example_id !== "string") return null;
  if (typeof score !== "number") return null;
  if (typeof feedback !== "string") return null;
  return {
    example_id,
    score,
    feedback,
    sequence,
    iteration: typeof iteration === "number" ? iteration : null,
    candidate_id: typeof metrics.candidate_id === "string" ? metrics.candidate_id : null,
    images: coerceImages(metrics.images),
    images_dropped: typeof images_dropped === "number" && images_dropped > 0 ? images_dropped : 0,
  };
}

function coerceImages(value: unknown): SideImage[] {
  if (!Array.isArray(value)) return [];
  const out: SideImage[] = [];
  for (const item of value) {
    if (typeof item !== "object" || item === null) continue;
    const { key, src } = item as Record<string, unknown>;
    if (typeof key !== "string" || typeof src !== "string") continue;
    if (!src.startsWith("data:image/")) continue;
    out.push({ key, src });
  }
  return out;
}

export function extractMinibatch(events: ProgressEvent[]): MinibatchEntry[] {
  const out: MinibatchEntry[] = [];
  let seq = 0;
  for (const event of events) {
    if (event.event !== MINIBATCH_EVENT) continue;
    const parsed = coerceMinibatch(event.metrics ?? {}, seq);
    if (parsed === null) continue;
    out.push(parsed);
    seq += 1;
  }
  return out;
}
