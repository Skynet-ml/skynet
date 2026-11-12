import type { BlackboxNamedScore, BlackboxRunResult } from "@/shared/types/api";

/** The final run's score of the starting version; older runs stored it as `baseline_test_metric`. */
export function blackboxBaselineScore(result: BlackboxRunResult | null | undefined): number | null {
  return result?.baseline_score ?? result?.baseline_test_metric ?? null;
}

/** The final run's score of the winner; older runs stored it as `optimized_test_metric`. */
export function blackboxBestScore(result: BlackboxRunResult | null | undefined): number | null {
  return result?.best_score ?? result?.optimized_test_metric ?? null;
}

/** Feedback text of a side-info mapping, when the scorer gave one. */
export function sideInfoFeedback(
  sideInfo: Record<string, unknown> | null | undefined,
): string | null {
  const feedback = sideInfo?.feedback;
  return typeof feedback === "string" && feedback.trim() ? feedback : null;
}

/**
 * Named scores carried in a side-info mapping. Current runs store
 * `{name: {score, feedback}}`; older runs may store `{name: number}`, read
 * with empty feedback. Anything else is skipped.
 */
export function sideInfoNamedScores(
  sideInfo: Record<string, unknown> | null | undefined,
): Record<string, BlackboxNamedScore> {
  const raw = sideInfo?.scores;
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) return {};
  const named: Record<string, BlackboxNamedScore> = {};
  for (const [name, value] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof value === "number") {
      named[name] = { score: value, feedback: "" };
    } else if (value != null && typeof value === "object") {
      const entry = value as Record<string, unknown>;
      if (typeof entry.score !== "number") continue;
      named[name] = {
        score: entry.score,
        feedback: typeof entry.feedback === "string" ? entry.feedback : "",
      };
    }
  }
  return named;
}
