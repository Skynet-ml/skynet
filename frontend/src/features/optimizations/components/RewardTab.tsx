"use client";

import { readRewardSpec } from "@/shared/lib/reward";
import { RewardPreview } from "@/shared/ui/reward/reward-preview";
import type { BlackboxRunResult } from "@/shared/types/api";
import { RewardComparison, savedMetricCode } from "./RewardComparison";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The metric code a run saved, from its workspace, scorer or top-level field. */
function runMetricCode(payload: unknown): string | null {
  const saved = savedMetricCode(payload);
  if (saved) return saved;
  const top = isRecord(payload) ? payload.metric_code : null;
  return typeof top === "string" ? top : null;
}

/** Whether a run's metric is an accuracy, speed and cost reward, so it gets a Reward tab. */
export function hasRewardTab(payload: unknown): boolean {
  return readRewardSpec(runMetricCode(payload)).kind === "spec";
}

/**
 * The run page's Reward tab (SKY-84). A finished run shows one table: each
 * part's weight beside the baseline and the selected version. Until then the
 * tab shows the metric's REWARD_SPEC parts.
 *
 * Args:
 *   payload: The run's saved submit payload.
 *   result: The black-box result, once the run finished.
 */
export function RewardTab({
  payload,
  result,
}: {
  payload: unknown;
  result: BlackboxRunResult | null;
}) {
  const code = runMetricCode(payload);
  if (result) return <RewardComparison result={result} metricCode={code} />;
  return (
    <div className="rounded-xl border border-border bg-card px-4 py-3.5">
      <RewardPreview read={readRewardSpec(code)} />
    </div>
  );
}
