import { test } from "node:test";
import assert from "node:assert/strict";

import {
  canReview,
  cycleTone,
  enableProblem,
  modelChanges,
  modelLabels,
  refreshDelayMs,
  scoreGain,
} from "./continuous.ts";
import type { ContinuousCycle, ContinuousPlan } from "@/shared/types/continuous";

test("the enable form needs the reflection model switch", () => {
  assert.equal(enableProblem({ permitted: { task_model: false, reflection_model: true } }), null);
  assert.equal(
    enableProblem({ permitted: { task_model: true, reflection_model: false } }),
    "continuous_tab.form.need_permission",
  );
});

test("a check's changes name each swapped model's old and new name", () => {
  const changed = {
    optimization_id: "run",
    changes: ["reflection_model", "task_model"],
    snapshot: {
      new_models: {
        task_model: { name: "openrouter/openai/gpt-6.1-sol", byok_provider: null },
        reflection_model: { name: "openrouter/anthropic/claude-sonnet-5-5", byok_provider: null },
      },
      old_models: {
        task_model: "openrouter/openai/gpt-6-luna",
        reflection_model: "openrouter/anthropic/claude-sonnet-5",
      },
    },
  } as unknown as ContinuousCycle;
  assert.deepEqual(modelChanges(changed), [
    {
      role: "reflection_model",
      from: "openrouter/anthropic/claude-sonnet-5",
      to: "openrouter/anthropic/claude-sonnet-5-5",
    },
    {
      role: "task_model",
      from: "openrouter/openai/gpt-6-luna",
      to: "openrouter/openai/gpt-6.1-sol",
    },
  ]);
  // A check saved before old names were kept still shows its new model.
  const noOld = {
    optimization_id: "run",
    changes: ["task_model"],
    snapshot: { new_models: changed.snapshot?.new_models },
  };
  assert.deepEqual(modelChanges(noOld as unknown as ContinuousCycle), [
    { role: "task_model", from: null, to: "openrouter/openai/gpt-6.1-sol" },
  ]);
  // A check that stopped before its run started swapped nothing, so it lists no change.
  assert.deepEqual(
    modelChanges({
      changes: ["task_model"],
      snapshot: null,
      optimization_id: null,
    } as unknown as ContinuousCycle),
    [],
  );
  assert.deepEqual(
    modelChanges({ ...changed, optimization_id: null } as unknown as ContinuousCycle),
    [],
  );
  assert.deepEqual(modelChanges({ changes: [], snapshot: null } as unknown as ContinuousCycle), []);
  // A check saved before its models were kept has no names to show, so it lists no role alone.
  assert.deepEqual(
    modelChanges({
      optimization_id: "run",
      changes: ["task_model"],
      snapshot: null,
    } as unknown as ContinuousCycle),
    [],
  );
});

test("model names drop the provider path unless that hides the change", () => {
  assert.deepEqual(modelLabels("openrouter/openai/gpt-6-luna", "openrouter/openai/gpt-6.1-sol"), [
    "gpt-6-luna",
    "gpt-6.1-sol",
  ]);
  assert.deepEqual(modelLabels(null, "openrouter/openai/gpt-6.1-sol"), [null, "gpt-6.1-sol"]);
  // Same model from another provider: the short names would read as no change.
  assert.deepEqual(modelLabels("openai/gpt-6.1-sol", "openrouter/openai/gpt-6.1-sol"), [
    "openai/gpt-6.1-sol",
    "openrouter/openai/gpt-6.1-sol",
  ]);
  assert.deepEqual(modelLabels("openai/", "openai/gpt-6.1-sol"), ["openai/", "openai/gpt-6.1-sol"]);
});

test("only a validated candidate with a run can be reviewed", () => {
  const cycle = {
    id: "c",
    slot: "s",
    month: "2026-10",
    status: "awaiting_review" as const,
    changes: ["data"],
    detail_code: null,
    detail_params: {},
    snapshot: null,
    optimization_id: "run",
    incumbent_score: 0.5,
    candidate_score: 0.7,
    spent_cents: 10,
    created_at: "2026-10-18T00:00:00Z",
  };
  assert.equal(canReview(cycle), true);
  assert.equal(canReview({ ...cycle, status: "regressed" }), false);
  assert.equal(canReview({ ...cycle, status: "skipped", optimization_id: null }), false);
  assert.equal(cycleTone("skipped"), "muted");
  assert.equal(cycleTone("failed"), "bad");
  assert.equal(cycleTone("regressed"), "warn");
});

test("the tab reloads while a check is due or running, waits for the next one, and stops when nothing is pending", () => {
  const now = Date.parse("2026-10-19T04:00:00Z");
  const plan = (patch: Partial<ContinuousPlan>, statuses: ContinuousCycle["status"][] = []) =>
    ({
      status: "active",
      next_check_at: "2026-10-20T05:30:00Z",
      cycles: statuses.map((status) => ({ status })),
      ...patch,
    }) as ContinuousPlan;
  assert.equal(refreshDelayMs(plan({}), now), 86_400_000);
  assert.equal(
    refreshDelayMs(plan({ next_check_at: "2026-10-19T05:00:00Z" }, ["skipped"]), now),
    3_605_000,
  );
  assert.equal(refreshDelayMs(plan({ status: "paused" }), now), null);
  assert.equal(refreshDelayMs(plan({ next_check_at: "2026-10-19T04:00:00Z" }), now), 5_000);
  assert.equal(refreshDelayMs(plan({}, ["checking"]), now), 5_000);
  assert.equal(refreshDelayMs(plan({}, ["optimizing"]), now), 30_000);
  assert.equal(
    refreshDelayMs(plan({ status: "paused", next_check_at: null }, ["skipped"]), now),
    null,
  );
});

test("scoreGain rounds to the shown precision so a tiny change reads as none", () => {
  assert.equal(
    scoreGain({ incumbent_score: 0.5, candidate_score: 0.512 } as ContinuousCycle),
    0.012,
  );
  assert.equal(
    scoreGain({ incumbent_score: 0.781, candidate_score: 0.744 } as ContinuousCycle),
    -0.037,
  );
  assert.equal(
    scoreGain({ incumbent_score: 0.6666667, candidate_score: 0.6666666 } as ContinuousCycle),
    0,
  );
  assert.equal(scoreGain({ incumbent_score: 0.7, candidate_score: 0.6996 } as ContinuousCycle), 0);
  assert.equal(scoreGain({ incumbent_score: null, candidate_score: 0.7 } as ContinuousCycle), null);
  // The gain is the difference of the shown scores: 0.812 -> 0.812 is no gain, 0.812 -> 0.813 is +0.001.
  assert.equal(
    scoreGain({ incumbent_score: 0.8116, candidate_score: 0.8124 } as ContinuousCycle),
    0,
  );
  assert.equal(
    scoreGain({ incumbent_score: 0.8124, candidate_score: 0.8126 } as ContinuousCycle),
    0.001,
  );
  // A half-way score: toFixed shows 0.1235 as 0.123, so 0.123 -> 0.125 is +0.002.
  assert.equal(
    scoreGain({ incumbent_score: 0.1235, candidate_score: 0.125 } as ContinuousCycle),
    0.002,
  );
});
