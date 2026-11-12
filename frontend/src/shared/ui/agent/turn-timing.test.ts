import assert from "node:assert/strict";
import { test } from "node:test";

import {
  blockWorkMs,
  earliestStart,
  finishedTurnMs,
  nextTurnClock,
  turnStart,
  visibleThinking,
} from "./turn-timing.ts";

test("a thinking segment with no reasoning is no row", () => {
  const segments = [
    { reasoning: "", startedAt: 1, endedAt: null },
    { reasoning: "plan", startedAt: 2, endedAt: 3 },
  ];
  assert.deepEqual(visibleThinking(segments), [segments[1]]);
});

test("the live timer's start only ever moves earlier", () => {
  // The reasoning-less segment that started the turn drops out of the rows;
  // the start it set must stay, or "Working for 2s" turns into "1s".
  let start = earliestStart(null, turnStart([{ startedAt: 1000, endedAt: null }], []));
  start = earliestStart(start, turnStart([], [{ call: { startedAt: 2000, endedAt: null } }]));
  assert.equal(start, 1000);
  assert.equal(earliestStart(start, null), 1000);
  assert.equal(earliestStart(null, null), null);
});

test("a finished turn counts up to the coding tool's end, after the reply", () => {
  const steps = [{ call: { startedAt: 1000, endedAt: 9000 } }];
  assert.equal(finishedTurnMs(1000, [], steps, 2000), 8000);
  assert.equal(finishedTurnMs(1000, [], [{ call: { startedAt: 1000, endedAt: 1500 } }], 2000), 2000);
  assert.equal(finishedTurnMs(null, [], [], null), 0);
});

test("a retried turn's clock starts at the retry, not at the failed attempt", () => {
  // Retry, regenerate and edit-and-resend put the new turn in the same
  // transcript slot, so its header kept the failed attempt's clock.
  const failed = { start: 1_000, streaming: false };
  const retried = nextTurnClock(failed, 346_000, true);
  assert.deepEqual(retried, { start: 346_000, streaming: true });
  assert.equal(
    finishedTurnMs(retried.start, [], [{ call: { startedAt: 346_000, endedAt: 367_000 } }]),
    21_000,
  );
});

// The setup agent's turn: thinking, a fetch_url call, more thinking, then a
// question for the user. The backend measured 15.6s.
const asked = {
  thinking: [
    { startedAt: 1_000, endedAt: 7_000 },
    { startedAt: 7_600, endedAt: 13_000 },
  ],
  steps: [{ call: { startedAt: 7_000, endedAt: 7_600 } }],
  durationMs: 15_600,
};
const HOUR = 3_600_000;

test("a finished turn that asked the user shows its own work", () => {
  const turn = { ...asked, start: turnStart(asked.thinking, asked.steps) };
  assert.equal(blockWorkMs([], turn, null), 15_600);
});

test("a work block counts each turn's own work, not the wait between them", () => {
  // The user answers on the wizard's next screen 57 minutes later; that
  // screen's turn joins the block.
  const later = 57 * 60_000;
  const next = {
    thinking: [{ startedAt: later, endedAt: later + 4_000 }],
    steps: [{ call: { startedAt: later + 4_000, endedAt: later + 5_000 } }],
    durationMs: 6_000,
  };
  assert.equal(blockWorkMs([asked], { ...next, start: later }, null), 21_600);
});

test("the hour the user took before the next turn never shows on its live timer", () => {
  const start = HOUR;
  const live = { thinking: [{ startedAt: start, endedAt: null }], steps: [], start };
  assert.equal(blockWorkMs([asked], live, start + 3_000), 18_600);
  assert.equal(blockWorkMs([], live, start + 3_000), 3_000);
  // A clock that reads before the start (a stale tick) shows no negative time.
  assert.equal(blockWorkMs([asked], live, start - 500), 15_600);
});

test("a running turn's clock keeps its earliest start", () => {
  const clock = { start: 1_000, streaming: true };
  assert.equal(nextTurnClock(clock, 1_000, true), clock);
  assert.equal(nextTurnClock(clock, 2_000, true), clock);
  assert.deepEqual(nextTurnClock(clock, 2_000, false), { start: 1_000, streaming: false });
  assert.deepEqual(nextTurnClock({ start: null, streaming: true }, 1_200, true), {
    start: 1_200,
    streaming: true,
  });
});
