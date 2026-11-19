"use client";

import type React from "react";
import type { OptimizationSummaryResponse } from "@/shared/types/api";
import { formatBlackboxDelta, formatBlackboxScore } from "@/shared/lib";
import { msg } from "@/shared/lib/messages";

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
    // score is better (SKY-750). The badge shows it as the run page does. The
    // summary has no direction, so a gain against the raw change marks a minimize run.
    const lowerIsBetter = improvement !== 0 && Math.sign(improvement) === -Math.sign(optimized - baseline);
    const color =
      improvement > 0
        ? "text-[var(--success)]"
        : improvement < 0
          ? "text-[var(--danger)]"
          : "text-muted-foreground";
    const delta = formatBlackboxDelta(improvement);
    const title = `${fmtScore(baseline)} → ${fmtScore(optimized)}`;
    return (
      <span
        className="inline-flex items-center gap-1 text-xs whitespace-nowrap"
        title={lowerIsBetter ? `${title} · ${msg("optimization.overview.lower_is_better")}` : title}
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
