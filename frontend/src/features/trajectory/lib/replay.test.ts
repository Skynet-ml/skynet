import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  agentStepCount,
  canStepBack,
  canStepForward,
  isStaleRequest,
  needsNextPage,
  shortTreeId,
} from "./replay.ts";

describe("replay position", () => {
  it("steps back only after the first step", () => {
    assert.equal(canStepBack(0), false);
    assert.equal(canStepBack(1), true);
  });

  it("steps forward inside the loaded steps or when the server has more", () => {
    assert.equal(canStepForward(0, 2, false), true);
    assert.equal(canStepForward(1, 2, false), false);
    assert.equal(canStepForward(1, 2, true), true);
  });

  it("asks for the next page only at the last loaded step", () => {
    assert.equal(needsNextPage(0, 2, true), false);
    assert.equal(needsNextPage(1, 2, true), true);
    assert.equal(needsNextPage(1, 2, false), false);
  });

  it("reads the step count of the shown agent", () => {
    const page = {
      agents: [
        { agent_id: "a", step_count: 3 },
        { agent_id: "b", step_count: 1 },
      ],
    };
    assert.equal(agentStepCount(page, "b"), 1);
    assert.equal(agentStepCount(page, "missing"), 0);
    assert.equal(agentStepCount(page, null), 0);
  });

  it("drops a response once a later load started", () => {
    assert.equal(isStaleRequest(3, 3), false);
    assert.equal(isStaleRequest(2, 3), true);
  });

  it("shortens a tree id to 12 characters", () => {
    assert.equal(shortTreeId("a".repeat(64)), "a".repeat(12));
  });
});
