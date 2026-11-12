import type { ReactNode } from "react";
import type { SideImage } from "@/shared/lib/candidate-render";

// Wire shape for trajectory events. Matches CandidateEvent.to_metrics() and
// RejectedEvent.to_metrics() in backend/core/service_gateway/optimization/trajectory.py.

// The backend keys candidates by their GEPA list index (0-based). The UI
// shifts this by 1 so the seed reads as candidate 1 rather than candidate 0.
export function displayCandidateId(id: string): string {
  const n = Number(id);
  return Number.isFinite(n) ? String(n + 1) : id;
}

export interface PerExampleScore {
  id: string;
  score: number;
}

export interface CandidateMetrics {
  candidate_id: string;
  parent_id: string | null;
  parents_extra: string[];
  generation: number;
  score: number;
  per_example: PerExampleScore[];
  prompt: Record<string, string>;
  discovered_at_evals: number;
  iteration: number | null;
  timestamp: string;
}

export interface RejectedMetrics {
  rejection_id: string;
  iteration: number;
  parent_id: string;
  parent_score: number;
  proposal_score: number;
  subsample_size: number;
  proposal_prompt: Record<string, string>;
  parent_prompt: Record<string, string>;
  subsample_ids: string[];
  per_example_parent: PerExampleScore[];
  per_example_proposal: PerExampleScore[];
}

export interface MinibatchEntry {
  example_id: string;
  score: number;
  feedback: string;
  sequence: number;
  // GEPA iteration the reflective propose() was on when this feedback fired.
  // null when the event predates iteration plumbing or fires outside a
  // propose() call (baseline / full-valset evaluation).
  iteration: number | null;
  // Version the note scored, set by engines without proposal iterations.
  candidate_id: string | null;
  // Renders the scorer attached, whole. They ride on the event outside the
  // feedback text cap; older events only had them inside the text.
  images: SideImage[];
  // Renders left out of the event by its byte budget.
  images_dropped: number;
}

export interface TrajectoryNode extends CandidateMetrics {
  children: TrajectoryNode[];
  x: number;
  y: number;
  subtreeWidth: number;
  isWinner: boolean;
  isSeed: boolean;
  isOnSpine: boolean;
}

export interface RejectedNode extends RejectedMetrics {
  x: number;
  y: number;
}

// Run configuration a black-box ("Optimize Anything") run hands the drawer, so
// it can name what a candidate is and decide which sections apply.
export interface BlackboxTrajectoryContext {
  // Whether candidates were scored against cases (per-case scores and notes
  // exist) or in single-task mode, where each version has one score.
  hasCases: boolean;
  // Scorer-attached renders keyed by candidate (see blackboxCandidateKey),
  // from the run's version history — present once the run has finished.
  rendersByText: ReadonlyMap<string, ReadonlyArray<{ key: string; src: string }>>;
  // A repository run's candidate is a patch, browsed as files; the run page
  // supplies the browser so the drawer shows the same one as the Versions tab.
  renderRepoVersion?: (
    prompt: Record<string, string>,
    parentPrompt: Record<string, string>,
  ) => ReactNode;
  // The scorer feedback the run saved on a version, for engines that never
  // stream per-case notes; null when that version has none.
  renderVersionFeedback?: (prompt: Record<string, string>) => ReactNode;
}

// GEPA stores a plain-string black-box candidate under this one synthetic key.
export const BLACKBOX_STR_CANDIDATE_KEY = "current_candidate";

// Identity of a black-box candidate shared by the tree, which sees the GEPA
// candidate map, and the run's version history, which sees the raw candidate.
export function blackboxCandidateKey(candidate: string | Record<string, string>): string {
  if (typeof candidate === "string") return candidate;
  const keys = Object.keys(candidate);
  const [only] = keys;
  if (keys.length === 1 && only === BLACKBOX_STR_CANDIDATE_KEY) return candidate[only] ?? "";
  return keys
    .sort()
    .map((key) => `${key}\u0000${candidate[key]}`)
    .join("\u0001");
}

// Black-box cases are keyed by validation index the way candidates are keyed
// by list index, and read 1-based for the same reason.
export function displayCaseId(id: string): string {
  return displayCandidateId(id);
}
