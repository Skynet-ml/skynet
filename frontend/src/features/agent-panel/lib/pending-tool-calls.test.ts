/** Contract: a pending tool row shows at once and the started call takes it over. */

import assert from "node:assert/strict";
import test from "node:test";

import type { AgentToolCall } from "../../../shared/ui/agent/types.ts";
import {
  dropPendingToolCalls,
  isPendingToolCall,
  pendingToolCall,
  placeToolCall,
  stopToolCalls,
} from "./pending-tool-calls.ts";

function started(id: string, tool: string, now: number): AgentToolCall {
  return { id, tool, reason: "", status: "running", startedAt: now, endedAt: null };
}

test("a started call takes over the first placeholder for its tool", () => {
  let calls = placeToolCall([], pendingToolCall("search", 0, 10));
  calls = placeToolCall(calls, pendingToolCall("fetch", 1, 11));
  calls = placeToolCall(calls, started("c1", "fetch", 50));
  assert.deepEqual(
    calls.map((row) => [row.id, row.tool, row.startedAt]),
    [
      ["pending:0", "search", 10],
      ["c1", "fetch", 11],
    ],
  );
});

test("a started call with no placeholder goes last", () => {
  const calls = placeToolCall([started("c1", "search", 1)], started("c2", "search", 2));
  assert.deepEqual(
    calls.map((row) => row.id),
    ["c1", "c2"],
  );
});

test("a stopped turn leaves no row running", () => {
  // A row left running kept the turn header's "Working for" clock going.
  const done: AgentToolCall = { ...started("c0", "fetch", 1), status: "done", endedAt: 2 };
  const calls = [done, started("c1", "search", 3), pendingToolCall("fetch", 0, 4)];
  assert.deepEqual(
    stopToolCalls(calls, 9).map((row) => [row.id, row.status, row.endedAt]),
    [
      ["c0", "done", 2],
      ["c1", "error", 9],
    ],
  );
});

test("placeholders that never started are dropped, started rows kept", () => {
  const calls = [started("c1", "search", 1), pendingToolCall("fetch", 0, 2)];
  assert.equal(isPendingToolCall(calls[1]), true);
  assert.deepEqual(
    dropPendingToolCalls(calls).map((row) => row.id),
    ["c1"],
  );
});
