import assert from "node:assert/strict";
import { test } from "node:test";

import { createStepViewTracker } from "./wizard-telemetry.ts";

test("a step view is reported once per change, never on a re-render", () => {
  const viewed = createStepViewTracker();
  const renders = ["task", "task", "inputs", "inputs", "inputs", "metric", "task", "task"] as const;
  const events = renders.map((step) => viewed(step, "guided")).filter(Boolean);
  assert.deepEqual(events, [
    { step: "task", index: 0, mode: "guided" },
    { step: "inputs", index: 1, mode: "guided" },
    { step: "metric", index: 3, mode: "guided" },
    { step: "task", index: 0, mode: "guided" },
  ]);
});

test("a step's index is its fixed position, whatever the mode", () => {
  const viewed = createStepViewTracker();
  assert.deepEqual(viewed("optimizer", "expert"), { step: "optimizer", index: 2, mode: "expert" });
  assert.equal(viewed("optimizer", "guided"), null);
});
