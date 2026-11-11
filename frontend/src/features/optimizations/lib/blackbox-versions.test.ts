import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { BlackboxRunResult } from "@/shared/types/api";

import {
  buildVersions,
  candidateToText,
  defaultVersionIndex,
} from "./blackbox-versions.ts";

function runResult(overrides: Partial<BlackboxRunResult>): BlackboxRunResult {
  return {
    optimizer_name: "gepa",
    strategy_mode: "auto",
    engine_used: "gepa",
    split_counts: {},
    baseline_test_metric: 0.1,
    optimized_test_metric: 0.9,
    metric_improvement: 0.8,
    seed_candidate: "seed",
    best_candidate: "best",
    regression_guard_applied: false,
    lanes: [],
    total_scorer_runs: 3,
    runtime_seconds: 1,
    num_lm_calls: 0,
    usage_by_model: {},
    optimization_metadata: {},
    details: {},
    ...overrides,
  } as BlackboxRunResult;
}

describe("buildVersions", () => {
  it("shows an unchanged repository as its starting version", () => {
    const versions = buildVersions(runResult({ seed_candidate: "", best_candidate: "", baseline_score: 0.8, best_score: 0.8 }));
    assert.equal(versions.length, 1);
    assert.equal(versions[0].isSeed, true);
    assert.equal(versions[0].isBest, true);
    assert.equal(versions[0].score, 0.8);
  });

  it("lays the history out as v0 seed then first-seen versions", () => {
    const versions = buildVersions(
      runResult({
        versions: [
          { candidate: "best", score: 0.9, evals: 2, first_run: 3, side_info: {} },
          {
            candidate: "seed",
            score: 0.1,
            evals: 1,
            first_run: 1,
            side_info: { feedback: "weak" },
          },
          { candidate: "worse", score: 0.05, evals: 1, first_run: 2, side_info: {} },
        ],
      }),
    );
    assert.deepEqual(
      versions.map((v) => [v.number, v.text, v.score, v.isSeed, v.isBest]),
      [
        [0, "seed", 0.1, true, false],
        [1, "worse", 0.05, false, false],
        [2, "best", 0.9, false, true],
      ],
    );
    assert.equal(versions[0].firstRun, 1);
    assert.deepEqual(versions[0].sideInfo, { feedback: "weak" });
    assert.equal(defaultVersionIndex(versions), 2);
  });

  it("collapses repeated texts and appends a best the history never saw", () => {
    const versions = buildVersions(
      runResult({
        versions: [
          { candidate: "seed", score: 0.1, evals: 1, first_run: 1, side_info: {} },
          { candidate: "seed", score: 0.1, evals: 1, first_run: 2, side_info: {} },
        ],
      }),
    );
    assert.deepEqual(
      versions.map((v) => [v.text, v.score, v.firstRun]),
      [
        ["seed", 0.1, 1],
        ["best", 0.9, null],
      ],
    );
    assert.equal(versions[1].isBest, true);
  });

  it("falls back to seed and best with held-out scores when the run has no history", () => {
    const versions = buildVersions(runResult({ versions: undefined }));
    assert.deepEqual(
      versions.map((v) => [v.number, v.text, v.score, v.evals]),
      [
        [0, "seed", 0.1, 0],
        [1, "best", 0.9, 0],
      ],
    );
  });

  it("flattens dict candidates into headed sections", () => {
    assert.equal(
      candidateToText({ system: "be kind", user: "hi" }),
      "## system\nbe kind\n\n## user\nhi",
    );
    const versions = buildVersions(
      runResult({
        seed_candidate: { system: "a" },
        best_candidate: { system: "b" },
        versions: [{ candidate: { system: "b" }, score: 1, evals: 1, first_run: 1, side_info: {} }],
      }),
    );
    assert.deepEqual(
      versions.map((v) => v.text),
      ["## system\na", "## system\nb"],
    );
  });
});

describe("buildVersions mean score", () => {
  it("carries the running mean beside the headline score", () => {
    const versions = buildVersions(
      runResult({
        versions: [
          {
            candidate: "seed",
            score: 0.2,
            mean_score: 0.15,
            evals: 3,
            first_run: 1,
            side_info: {},
          },
          { candidate: "best", score: 0.9, evals: 2, first_run: 2, side_info: {} },
        ],
      }),
    );

    assert.deepEqual(
      versions.map((v) => [v.text, v.score, v.meanScore]),
      [
        ["seed", 0.2, 0.15],
        ["best", 0.9, null],
      ],
    );
  });
});
