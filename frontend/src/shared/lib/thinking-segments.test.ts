import assert from "node:assert/strict";
import { test } from "node:test";

import {
  appendReasoning,
  closeThinking,
  interleaveThinking,
  lastEventAt,
  latestHeading,
  mergeAdjacentThinking,
  reopenThinking,
  parseThinkingRows,
  splitSummary,
  startThinking,
  summarizeThinking,
  thoughtSeconds,
  type ThinkingSegment,
} from "./thinking-segments.ts";

test("reasoning, a tool, then reasoning makes two segments with the tool between", () => {
  let segs: readonly ThinkingSegment[] = startThinking([], 0, 0);
  segs = appendReasoning(segs, "Let me ", 1000, 0);
  segs = appendReasoning(segs, "look.", 2000, 0);
  segs = closeThinking(segs, 4000);
  segs = appendReasoning(segs, "Found it.", 6000, 1);
  segs = closeThinking(segs, 9000);
  assert.deepEqual(segs, [
    { reasoning: "Let me look.", startedAt: 1000, endedAt: 4000, toolsBefore: 0 },
    { reasoning: "Found it.", startedAt: 6000, endedAt: 9000, toolsBefore: 1 },
  ]);
  const pieces = interleaveThinking(segs, [{ index: 0 }]);
  assert.deepEqual(
    pieces.map((p) => (p.kind === "thinking" ? `think:${p.segment.reasoning}` : p.kind === "steps" ? `steps:${p.steps.length}` : "card")),
    ["think:Let me look.", "steps:1", "think:Found it."],
  );
  assert.deepEqual(
    segs.map((s) => (s.endedAt ?? 0) - (s.startedAt ?? 0)),
    [3000, 3000],
  );
});

test("consecutive deltas merge into the open segment", () => {
  let segs: ThinkingSegment[] = [];
  segs = appendReasoning(segs, "a", 10, 0);
  segs = appendReasoning(segs, "b", 20, 0);
  segs = appendReasoning(segs, "c", 30, 0);
  assert.deepEqual(segs, [{ reasoning: "abc", startedAt: 10, endedAt: null, toolsBefore: 0 }]);
});

test("closing an empty placeholder drops it, and closing twice keeps the first end", () => {
  assert.deepEqual(closeThinking(startThinking([], 0, 0), 5), []);
  const once = closeThinking(appendReasoning([], "x", 1, 0), 5);
  assert.equal(closeThinking(once, 9), once);
  assert.equal(once[0]?.endedAt, 5);
});

test("a taken-back reply reopens the segment only when no tool ran since", () => {
  const closed = closeThinking(appendReasoning([], "x", 1, 0), 5);
  assert.equal(reopenThinking(closed, 0)[0]?.endedAt, null);
  assert.equal(reopenThinking(closed, 1), closed);
});

test("steps between two segments stay one burst", () => {
  const segs = [{ toolsBefore: 0 }, { toolsBefore: 2 }, { toolsBefore: 3 }];
  const pieces = interleaveThinking(segs, [{ index: 0 }, { index: 1 }, { index: 2 }, { index: 3 }]);
  assert.deepEqual(
    pieces.map((p) => (p.kind === "thinking" ? `t${p.segment.toolsBefore}` : p.kind === "steps" ? p.steps.map((s) => s.index).join("") : "card")),
    ["t0", "01", "t2", "2", "t3", "3"],
  );
});

test("a segment anchored to a hidden step still lands before the next shown one", () => {
  const pieces = interleaveThinking([{ toolsBefore: 1 }], [{ index: 0 }, { index: 2 }]);
  assert.deepEqual(
    pieces.map((p) => (p.kind === "thinking" ? "t" : p.kind === "steps" ? p.steps.map((s) => s.index).join("") : "card")),
    ["0", "t", "2"],
  );
});

test("reasoning that lands in one burst after a tool is timed from the tool's end", () => {
  let segs: readonly ThinkingSegment[] = closeThinking(appendReasoning([], "plan", 0, 0), 1000);
  const tools = [{ startedAt: 1000, endedAt: 1500 }];
  const since = lastEventAt(segs, tools);
  assert.equal(since, 1500);
  segs = appendReasoning(segs, "whole thought at once", 4000, 1, since);
  segs = closeThinking(segs, 4000);
  assert.equal(segs[1]?.startedAt, 1500);
  assert.equal(thoughtSeconds(segs[1]?.startedAt ?? null, segs[1]?.endedAt ?? 0), 2.5);
});

test("an open placeholder still starts at the first token", () => {
  const segs = appendReasoning(startThinking([], 0, 0), "a", 700, 0, 0);
  assert.equal(segs[0]?.startedAt, 700);
});

test("lastEventAt takes a running tool's start and is null for a fresh turn", () => {
  assert.equal(lastEventAt([], []), null);
  assert.equal(lastEventAt([], [{ startedAt: 300, endedAt: null }]), 300);
});

test("a segment with no measurable time has no seconds to show", () => {
  assert.equal(thoughtSeconds(1000, 1000), null);
  assert.equal(thoughtSeconds(1000, 1040), null);
  assert.equal(thoughtSeconds(null, 1000), null);
  assert.equal(thoughtSeconds(1000, 1060), 0.1);
});

test("a card keeps its place in the turn: think, tools, card, think, then more tools", () => {
  // The coding tool's card ran third; the reasoning after it and the call
  // after that must follow it, not jump above it or fall below the answer.
  const segs = [{ toolsBefore: 0 }, { toolsBefore: 3 }];
  const pieces = interleaveThinking(segs, [
    { index: 0 },
    { index: 1 },
    { index: 2, card: true },
    { index: 3 },
  ]);
  assert.deepEqual(
    pieces.map((p) =>
      p.kind === "thinking"
        ? `t${p.segment.toolsBefore}`
        : p.kind === "steps"
          ? p.steps.map((s) => s.index).join("")
          : `card${p.step.index}`,
    ),
    ["t0", "01", "card2", "t3", "3"],
  );
});

test("a card splits a burst of steps around it", () => {
  const pieces = interleaveThinking([], [{ index: 0 }, { index: 1, card: true }, { index: 2 }]);
  assert.deepEqual(
    pieces.map((p) => (p.kind === "steps" ? p.steps.map((s) => s.index).join("") : p.kind)),
    ["0", "card", "2"],
  );
});

test("a summary lands on the turn's n-th segment with reasoning, across a steer's messages", () => {
  const seg = (reasoning: string): ThinkingSegment => ({
    reasoning,
    startedAt: 0,
    endedAt: 1,
    toolsBefore: 0,
  });
  const messages = [
    { thinkingSegments: [seg("an earlier turn")] },
    { thinkingSegments: [seg("first"), seg("")] },
    {},
    { thinkingSegments: [seg("second")] },
  ];
  const once = summarizeThinking(messages, 1, 1, "Read the steer");
  assert.equal(once[3]?.thinkingSegments?.[0]?.summary, "Read the steer");
  assert.equal(once[1], messages[1]);
  assert.equal(once[0]?.thinkingSegments?.[0]?.summary, undefined);
  assert.equal(summarizeThinking(messages, 1, 2, "none"), messages);
});

test("saved thinking rows come back closed, malformed ones dropped", () => {
  assert.deepEqual(
    parseThinkingRows([
      { reasoning: "Pairs fit.", startedAt: 1, endedAt: 4, toolsBefore: 0, summary: "Picked pairs" },
      { reasoning: "", startedAt: 1, endedAt: 2, toolsBefore: 0 },
      { reasoning: "No times", toolsBefore: "x" },
      "junk",
    ]),
    [
      { reasoning: "Pairs fit.", startedAt: 1, endedAt: 4, toolsBefore: 0, summary: "Picked pairs" },
      { reasoning: "No times", startedAt: null, endedAt: 0, toolsBefore: 0 },
    ],
  );
  assert.equal(parseThinkingRows(null), undefined);
  assert.equal(parseThinkingRows([]), undefined);
});

test("a label splits into its heading and sentence; an old one-line label has no heading", () => {
  assert.deepEqual(splitSummary("Matching columns\nThe question column is the input."), {
    heading: "Matching columns",
    detail: "The question column is the input.",
  });
  assert.deepEqual(splitSummary("Your rows are pairs."), { detail: "Your rows are pairs." });
  assert.deepEqual(splitSummary("Matching columns\n"), { detail: "Matching columns" });
});

test("the live heading is the newest one, from bold reasoning lines before labels", () => {
  const seg = (reasoning: string, summary?: string) => ({ reasoning, summary });
  assert.equal(latestHeading([]), null);
  assert.equal(latestHeading([seg("plain", "Your rows are pairs.")]), null);
  assert.equal(
    latestHeading([seg("a", "Matching columns\nThe question column is the input."), seg("b")]),
    "Matching columns",
  );
  assert.equal(
    latestHeading([
      seg("x", "Old step\nDone."),
      seg("**Reading rows**\nSome text\n**Picking a metric**\nMore", "Labelled\nLater."),
    ]),
    "Picking a metric",
  );
});

test("thinking rows left side by side by a dropped step join into one row", () => {
  const segs = [
    { reasoning: "First.", startedAt: 1000, endedAt: 6000, toolsBefore: 0 },
    { reasoning: "Second.", startedAt: 9000, endedAt: 13000, toolsBefore: 1 },
    { reasoning: "Third.", startedAt: 20000, endedAt: 22000, toolsBefore: 2 },
  ];
  // Step 0 rendered nothing and dropped out, so rows 1 and 2 touch.
  const pieces = mergeAdjacentThinking(interleaveThinking(segs, [{ index: 1 }]));
  assert.deepEqual(pieces, [
    {
      kind: "thinking",
      // 5s plus 4s of thinking; the 3s gap between them does not count.
      segment: { reasoning: "First.\n\nSecond.", startedAt: 4000, endedAt: 13000, toolsBefore: 0 },
    },
    { kind: "steps", steps: [{ index: 1 }] },
    { kind: "thinking", segment: segs[2] },
  ]);
});
