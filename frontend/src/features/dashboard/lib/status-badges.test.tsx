/*
 * node --test cannot load .tsx, so this runs through jiti (status-badges.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS='{"@/":"<abs>/src/"}' npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { OptimizationSummaryResponse } from "@/shared/types/api";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { formatScore } = await import("./status-badges");

function scoreText(
  optimizationType: OptimizationSummaryResponse["optimization_type"],
  baseline: number | null,
  optimized: number | null,
): string {
  const job = {
    optimization_type: optimizationType,
    baseline_test_metric: baseline,
    optimized_test_metric: optimized,
    metric_improvement:
      baseline != null && optimized != null ? Number((optimized - baseline).toFixed(6)) : null,
  } as OptimizationSummaryResponse;
  const node = formatScore(job);
  if (typeof node === "string") return node;
  // The spans sit apart through a CSS gap, so a tag boundary reads as a space.
  return renderToStaticMarkup(<>{node}</>)
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

void describe("formatScore", () => {
  void it("shows a black-box 0–1 score and its gain as raw values", () => {
    assert.equal(scoreText("blackbox", 0, 1), "1 (+1)");
    assert.equal(scoreText("blackbox", 0.5, 0.84), "0.84 (+0.34)");
    assert.equal(scoreText("blackbox", 0.9, 0.75), "0.75 (-0.15)");
  });

  void it("shows a black-box score without a baseline as a raw value", () => {
    assert.equal(scoreText("blackbox", null, 0.84), "0.84");
    assert.equal(scoreText("blackbox", 0.84, null), "0.84");
  });

  void it("puts the baseline and the optimized score in the hover title", () => {
    const job = {
      optimization_type: "blackbox",
      baseline_test_metric: 0,
      optimized_test_metric: 1,
      metric_improvement: 1,
    } as OptimizationSummaryResponse;
    assert.match(renderToStaticMarkup(<>{formatScore(job)}</>), /title="0 → 1"/);
  });

  void it("shows a dash when the run has no score", () => {
    assert.equal(scoreText("blackbox", null, null), "-");
  });
});
