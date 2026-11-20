import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { ProgressEvent } from "@/shared/types/api";
import type { CandidateMetrics } from "./types";

import {
  CLIMB_LAYOUT,
  agentRunKey,
  buildClimb,
  engineOfLatestLane,
  extractAgentRuns,
  extractCaseScores,
  finalRunKey,
  indexAgentRuns,
  climbEngineOf,
  latestVersionLane,
  layoutClimb,
  pendingCases,
  scopeToVersionLane,
  type AgentRunSummary,
} from "./meta-harness.ts";

function event(name: string, metrics: Record<string, unknown> = {}): ProgressEvent {
  return { timestamp: "2026-01-01T00:00:00Z", event: name, metrics };
}

function versionRun(runId: number, trial: number, exampleId: string): AgentRunSummary {
  return {
    run_id: runId,
    phase: "version",
    trial,
    example_id: exampleId,
    case_id: null,
    label: `run-${runId}`,
    status: "running",
    exit_code: null,
    timed_out: false,
    error: null,
    elapsed_seconds: null,
  };
}

function candidate(
  id: string,
  score: number,
  parent: string | null,
  perExample: Array<[string, number]> = [],
): CandidateMetrics {
  return {
    candidate_id: id,
    parent_id: parent,
    parents_extra: [],
    generation: parent === null ? 0 : 1,
    score,
    per_example: perExample.map(([caseId, caseScore]) => ({ id: caseId, score: caseScore })),
    prompt: { current_candidate: `v${id}` },
    discovered_at_evals: 0,
    iteration: Number(id),
    timestamp: "2026-01-01T00:00:00Z",
  };
}

describe("latestVersionLane", () => {
  it("counts case scores as version events", () => {
    const events = [
      event("candidate", { candidate_id: "0", lane_index: 0 }),
      event("case_scored", { trial: 0, example_id: "0", score: 1, total: 1, lane_index: 2 }),
    ];
    assert.equal(latestVersionLane(events), 2);
  });

  it("scopes version events to that lane and passes the rest through", () => {
    const events = [
      event("candidate", { candidate_id: "0", lane_index: 0 }),
      event("lane_completed", { engine: "gepa" }),
      event("case_scored", { trial: 0, example_id: "0", score: 1, total: 1, lane_index: 1 }),
      event("minibatch_feedback", { lane_index: 0 }),
    ];
    assert.deepEqual(
      scopeToVersionLane(events).map((e) => e.event),
      ["lane_completed", "case_scored"],
    );
  });

  it("keeps held-out agent runs of every lane but version runs of the newest only", () => {
    const events = [
      event("agent_run", { run_id: 1, phase: "baseline", status: "finished", lane_index: 0 }),
      event("agent_run", { run_id: 2, phase: "version", status: "finished", lane_index: 0 }),
      event("case_scored", { trial: 0, example_id: "0", score: 1, total: 1, lane_index: 1 }),
      event("agent_run", { run_id: 3, phase: "version", status: "running", lane_index: 1 }),
    ];
    assert.deepEqual(
      scopeToVersionLane(events).map((e) => e.metrics.run_id ?? e.event),
      [1, "case_scored", 3],
    );
  });
});

describe("extractAgentRuns", () => {
  it("keeps the latest summary per run in start order and drops malformed ones", () => {
    const events = [
      event("agent_run", {
        run_id: 1,
        phase: "version",
        trial: 2,
        example_id: 0,
        case_id: "unicorn",
        label: "version 3",
        status: "running",
      }),
      event("agent_run", { run_id: 2, phase: "baseline", example_id: "1", status: "running" }),
      event("agent_run", { run_id: "3", phase: "version", status: "running" }),
      event("agent_run", { run_id: 4, phase: "version" }),
      event("agent_run", {
        run_id: 1,
        phase: "version",
        trial: 2,
        example_id: "0",
        status: "finished",
        exit_code: 0,
        elapsed_seconds: 12.5,
      }),
    ];
    const runs = extractAgentRuns(events);
    assert.deepEqual(
      runs.map((run) => [run.run_id, run.status]),
      [
        [1, "finished"],
        [2, "running"],
      ],
    );
    assert.deepEqual(runs[0], {
      run_id: 1,
      phase: "version",
      trial: 2,
      example_id: "0",
      case_id: null,
      label: "",
      status: "finished",
      exit_code: 0,
      timed_out: false,
      error: null,
      elapsed_seconds: 12.5,
    });
    assert.equal(runs[1]?.trial, null);
  });

  it("indexes runs by version and case, with the starting point as version 0", () => {
    const runs = extractAgentRuns([
      event("agent_run", {
        run_id: 1,
        phase: "version",
        trial: 1,
        example_id: "1",
        status: "running",
      }),
      event("agent_run", { run_id: 2, phase: "baseline", example_id: "1", status: "running" }),
      event("agent_run", { run_id: 3, phase: "version", example_id: "1", status: "running" }),
      event("agent_run", { run_id: 4, phase: "final", example_id: "1", status: "finished" }),
    ]);
    const byCell = indexAgentRuns(runs);
    assert.deepEqual(
      [...byCell.keys()],
      [agentRunKey(1, "1"), agentRunKey(0, "1"), finalRunKey("1")],
    );
    assert.equal(byCell.get("1:1")?.run_id, 1);
    assert.equal(byCell.get("0:1")?.run_id, 2);
    assert.equal(byCell.get("final:1")?.run_id, 4);
  });
});

describe("engineOfLatestLane", () => {
  it("reads the engine from the lane_started event of the newest lane", () => {
    const events = [
      event("lane_started", { engine: "gepa", phase: "explore" }),
      event("candidate", { candidate_id: "0", lane_index: 0 }),
      event("lane_started", { engine: "meta_harness", phase: "continue" }),
      event("case_scored", { trial: 0, example_id: "0", score: 1, total: 2, lane_index: 1 }),
    ];
    assert.equal(engineOfLatestLane(events), "meta_harness");
    assert.equal(climbEngineOf(events, "gepa", "gepa"), "meta_harness");
  });

  it("falls back to the result's engine, then the strategy's, without lane events", () => {
    assert.equal(engineOfLatestLane([]), null);
    assert.equal(climbEngineOf([], "meta_harness", null), "meta_harness");
    assert.equal(climbEngineOf([], null, "autosaddler"), "autosaddler");
    assert.equal(climbEngineOf([], "shinka_evolve", "gepa"), "shinka_evolve");
    assert.equal(climbEngineOf([], "autoresearch", "gepa"), "autoresearch");
    assert.equal(climbEngineOf([], "gepa", "meta_harness"), null);
    assert.equal(climbEngineOf([], "best_of_n", null), null);
    assert.equal(climbEngineOf([], null, null), null);
  });
});

describe("extractCaseScores", () => {
  it("keeps well-formed case scores and coerces numeric case ids", () => {
    const events = [
      event("case_scored", { trial: 1, example_id: 3, score: 0.5, total: 4 }),
      event("case_scored", { trial: "1", example_id: "0", score: 0.5, total: 4 }),
      event("case_scored", { trial: 1, example_id: "0", score: "high", total: 4 }),
      event("candidate", { candidate_id: "0" }),
    ];
    assert.deepEqual(extractCaseScores(events), [
      { trial: 1, example_id: "3", score: 0.5, total: 4 },
    ]);
  });
});

describe("buildClimb", () => {
  it("SKY-750: marks the versions that go lower for a minimize run", () => {
    const model = buildClimb(
      [candidate("0", 0.5, null, []), candidate("1", 0.3, "0", []), candidate("2", 0.4, "1", [])],
      [],
      [],
      true,
    );
    assert.deepEqual(
      model.versions.map((v) => [v.index, v.improved]),
      [
        [0, true],
        [1, true],
        [2, false],
      ],
    );
    assert.equal(model.bestId, "1");
  });

  it("marks the versions that beat the best before them and keeps the first best", () => {
    const model = buildClimb(
      [
        candidate("2", 0.8, "1", [["0", 1]]),
        candidate("0", 0.5, null, [["0", 0.5]]),
        candidate("1", 0.8, "0", [["1", 0.8]]),
        candidate("3", 0.4, "1", [["10", 0.4]]),
      ],
      [],
    );
    assert.deepEqual(
      model.versions.map((v) => [v.index, v.improved, v.bestBefore]),
      [
        [0, true, null],
        [1, true, 0.5],
        [2, false, 0.8],
        [3, false, 0.8],
      ],
    );
    assert.equal(model.bestId, "1");
    assert.deepEqual(model.caseIds, ["0", "1", "10"]);
    assert.equal(model.pending, null);
  });

  it("collects the cases of the version scored after the last complete one", () => {
    const model = buildClimb(
      [candidate("0", 0.5, null, [["0", 0.5]])],
      [
        { trial: 0, example_id: "0", score: 0.5, total: 2 },
        { trial: 1, example_id: "1", score: 0.9, total: 2 },
        { trial: 1, example_id: "0", score: 0.7, total: 2 },
      ],
    );
    assert.notEqual(model.pending, null);
    assert.equal(model.pending?.index, 1);
    assert.equal(model.pending?.total, 2);
    assert.deepEqual(
      [...(model.pending?.scores.entries() ?? [])],
      [
        ["1", 0.9],
        ["0", 0.7],
      ],
    );
    assert.deepEqual(model.caseIds, ["0", "1"]);
  });

  it("ignores case scores of versions that already completed", () => {
    const model = buildClimb(
      [candidate("0", 0.5, null), candidate("1", 0.6, "0")],
      [{ trial: 1, example_id: "0", score: 0.6, total: 1 }],
    );
    assert.equal(model.pending, null);
  });

  it("announces the pending version from its running agent runs before any case is scored", () => {
    const model = buildClimb(
      [
        candidate("0", 0.5, null, [
          ["0", 0.5],
          ["1", 0.5],
        ]),
      ],
      [],
      [versionRun(7, 1, "0"), versionRun(8, 1, "1"), versionRun(3, 0, "1")],
    );
    assert.equal(model.pending?.index, 1);
    assert.equal(model.pending?.total, 2);
    assert.equal(model.pending?.scores.size, 0);
    assert.deepEqual(pendingCases(model), [
      { id: "0", score: null },
      { id: "1", score: null },
    ]);
  });

  it("lists the pending version's cases with the scores in so far", () => {
    const model = buildClimb(
      [
        candidate("0", 0.5, null, [
          ["0", 0.5],
          ["1", 0.5],
          ["2", 0.5],
        ]),
      ],
      [{ trial: 1, example_id: "1", score: 0.9, total: 3 }],
      [versionRun(9, 1, "2")],
    );
    assert.deepEqual(pendingCases(model), [
      { id: "0", score: null },
      { id: "1", score: 0.9 },
      { id: "2", score: null },
    ]);
    assert.deepEqual(pendingCases(buildClimb([candidate("0", 0.5, null)], [])), []);
  });
});

describe("layoutClimb", () => {
  const model = buildClimb(
    [
      candidate("0", 0.2, null),
      candidate("1", 0.6, "0"),
      candidate("2", 0.4, "1"),
      candidate("3", 0.9, "1"),
    ],
    [{ trial: 4, example_id: "0", score: 0.5, total: 3 }],
  );

  it("places versions left to right at a pitch that fills the width", () => {
    const narrow = layoutClimb(model);
    assert.deepEqual(
      narrow.points.map((p) => p.x),
      [0, 1, 2, 3].map((i) => CLIMB_LAYOUT.padStart + i * CLIMB_LAYOUT.stepMin),
    );
    const wide = layoutClimb(model, { availableWidth: 10_000 });
    const pitch = (wide.points[1]?.x ?? 0) - (wide.points[0]?.x ?? 0);
    assert.equal(pitch, CLIMB_LAYOUT.stepMax);
    assert.equal(
      wide.width,
      CLIMB_LAYOUT.padStart + 3 * CLIMB_LAYOUT.stepMax + CLIMB_LAYOUT.padEnd,
    );
  });

  it("grows the plot to the height on hand, never below its natural height", () => {
    const natural = layoutClimb(model);
    const naturalHeight = CLIMB_LAYOUT.padTop + CLIMB_LAYOUT.plotHeight + CLIMB_LAYOUT.padBottom;
    assert.equal(natural.height, naturalHeight);
    assert.equal(layoutClimb(model, { availableHeight: 100 }).height, naturalHeight);
    const tall = layoutClimb(model, { availableHeight: 1_000 });
    assert.equal(tall.height, 1_000);
    assert.equal(tall.ticks[0]?.y, 1_000 - CLIMB_LAYOUT.padBottom);
    assert.equal(tall.ticks[CLIMB_LAYOUT.tickCount - 1]?.y, CLIMB_LAYOUT.padTop);
  });

  it("uses the unit scale when every score is a fraction and ranks higher scores higher", () => {
    const layout = layoutClimb(model);
    assert.deepEqual(layout.domain, { min: 0, max: 1, unit: true });
    const ys = layout.points.map((p) => p.y);
    assert.ok((ys[1] ?? 0) < (ys[0] ?? 0));
    assert.ok((ys[2] ?? 0) > (ys[1] ?? 0));
    assert.ok((ys[3] ?? 0) < (ys[1] ?? 0));
    assert.equal(layout.ticks.length, CLIMB_LAYOUT.tickCount);
    assert.equal(layout.ticks[0]?.value, 0);
    assert.equal(layout.ticks[CLIMB_LAYOUT.tickCount - 1]?.value, 1);
  });

  it("pads a free-form scale around the scores", () => {
    const wide = buildClimb([candidate("0", 10, null), candidate("1", 30, "0")], []);
    const layout = layoutClimb(wide);
    assert.equal(layout.domain.unit, false);
    assert.ok(layout.domain.min < 10);
    assert.ok(layout.domain.max > 30);
  });

  it("links every version to the one it was rewritten from", () => {
    const layout = layoutClimb(model);
    assert.equal(layout.edges.length, 3);
    assert.deepEqual(
      layout.edges.map((e) => [e.from.id, e.to.id]),
      [
        ["0", "1"],
        ["1", "2"],
        ["1", "3"],
      ],
    );
  });

  it("adds the pending version as a last column only when asked", () => {
    assert.equal(layoutClimb(model).pending, null);
    const layout = layoutClimb(model, { showPending: true });
    assert.notEqual(layout.pending, null);
    assert.equal(layout.pending?.x, CLIMB_LAYOUT.padStart + 4 * CLIMB_LAYOUT.stepMin);
    assert.equal(
      layout.width,
      CLIMB_LAYOUT.padStart + 4 * CLIMB_LAYOUT.stepMin + CLIMB_LAYOUT.padEnd,
    );
  });
});
