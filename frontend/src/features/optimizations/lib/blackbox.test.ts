import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { CandidateMetrics } from "@/features/trajectory";

import { extractBlackboxScorePoints } from "./blackbox.ts";

function candidate(id: string, score: number): CandidateMetrics {
  return {
    candidate_id: id,
    parent_id: null,
    parents_extra: [],
    generation: 0,
    score,
    per_example: [],
    prompt: {},
    discovered_at_evals: 0,
    iteration: null,
    timestamp: "2026-01-01T00:00:00Z",
  };
}

describe("extractBlackboxScorePoints", () => {
  it("plots one point per version at its mean, with the best mean so far", () => {
    const points = extractBlackboxScorePoints([
      candidate("0", 0.5875),
      candidate("1", 0.5),
      candidate("2", 0.6),
      candidate("3", 0.1),
    ]);
    assert.deepEqual(points, [
      { trial: 0, score: 0.5875, best: 0.5875 },
      { trial: 1, score: 0.5, best: 0.5875 },
      { trial: 2, score: 0.6, best: 0.6 },
      { trial: 3, score: 0.1, best: 0.6 },
    ]);
  });

  it("keeps the lowest mean so far for a minimize run", () => {
    const points = extractBlackboxScorePoints([candidate("0", 0.5), candidate("1", 0.7), candidate("2", 0.2)], true);
    assert.deepEqual(
      points.map((p) => p.best),
      [0.5, 0.5, 0.2],
    );
  });

  it("numbers versions by their id, falling back to their position", () => {
    const points = extractBlackboxScorePoints([candidate("0", 0.2), candidate("gepa-a", 0.4)]);
    assert.deepEqual(
      points.map((p) => p.trial),
      [0, 1],
    );
  });
});
