import type {
  BlackboxCandidate,
  BlackboxRunResult,
  OptimizationPayloadResponse,
} from "@/shared/types/api";
import type { ScoreChartArtifact } from "@/shared/ui/score-chart";

function candidateOf(value: unknown): BlackboxCandidate | null {
  return typeof value === "string" || (typeof value === "object" && value !== null)
    ? (value as BlackboxCandidate)
    : null;
}

/**
 * Name what a black-box run's versions are, for the score chart: the starting
 * point (or the result's candidate while the payload is still loading) as one
 * file, one text, or a bare version when it has several named parts.
 */
export function describeBlackboxArtifact(
  result: BlackboxRunResult | null,
  payload: OptimizationPayloadResponse | null,
): ScoreChartArtifact {
  const submitted = payload?.payload ?? {};
  const candidate =
    candidateOf(submitted.seed_candidate) ??
    result?.seed_candidate ??
    result?.best_candidate ??
    null;
  if (candidate == null) return { kind: "version" };
  if (typeof candidate === "string") return { kind: "text", text: candidate };
  const names = Object.keys(candidate);
  const [name] = names;
  return names.length === 1 && name ? { kind: "file", name } : { kind: "version" };
}
