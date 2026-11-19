"use client";

import type React from "react";
import type { OptimizationSummaryResponse } from "@/shared/types/api";
import { formatBlackboxDelta, formatBlackboxScore } from "@/shared/lib";

export function formatScore(job: OptimizationSummaryResponse): React.ReactNode {
  const baseline = job.baseline_test_metric;
  const optimized = job.optimized_test_metric;
  const improvement = job.metric_improvement;

  if (baseline == null && optimized == null) return "-";

  // Runs report raw scorer values, often 0–1, not a 0–100 percentage; format
  // them the way the run page does.
  const fmtScore = formatBlackboxScore;

  if (baseline != null && optimized != null && improvement != null) {
    // metric_improvement is positive when the run got better, also when a lower
    // score is better (SKY-750). It sets the color; the text shows the raw change.
    const color =
      improvement > 0
        ? "text-[var(--success)]"
        : improvement < 0
          ? "text-[var(--danger)]"
          : "text-muted-foreground";
    const delta = formatBlackboxDelta(optimized - baseline);
    return (
      <span
        className="inline-flex items-center gap-1 text-xs whitespace-nowrap"
        title={`${fmtScore(baseline)} → ${fmtScore(optimized)}`}
      >
        <span className="font-medium">{fmtScore(optimized)}</span>
        <span className={`${color} font-medium`}>({delta})</span>
      </span>
    );
  }

  if (optimized != null) return <span className="font-medium text-xs">{fmtScore(optimized)}</span>;
  if (baseline != null)
    return <span className="text-muted-foreground text-xs">{fmtScore(baseline)}</span>;
  return "-";
}
