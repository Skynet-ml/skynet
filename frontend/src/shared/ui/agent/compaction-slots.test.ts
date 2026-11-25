import assert from "node:assert/strict";
import { test } from "node:test";

import { compactionSlots, placeSlots } from "./compaction-slots.ts";
import type { AgentMessage, ChatCompaction } from "./types";

const entry = (reason: string, keptTurns: number): ChatCompaction =>
  ({ summary: "s", reason, summarizedTurns: 2, keptTurns, cents: 1 }) as ChatCompaction;
const user = (content: string, compaction?: ChatCompaction): AgentMessage => ({
  role: "user",
  content,
  ...(compaction ? { compaction } : {}),
});
const agent = (content: string): AgentMessage => ({ role: "assistant", content });

test("a /compact row shows after the last message it kept, at the bottom of the chat", () => {
  const messages = [user("a"), agent("b"), user("c", entry("manual", 2)), agent("d")];
  assert.deepEqual(
    compactionSlots(messages).map((s) => [s.anchor, s.after]),
    [[2, 3]],
  );
});

test("a threshold row shows after the user message that started the turn", () => {
  const messages = [user("a", entry("threshold", 2)), agent("b"), user("c"), agent("d")];
  assert.deepEqual(
    compactionSlots(messages).map((s) => s.after),
    [2],
  );
});

test("each row has its own anchor, so React keys stay unique", () => {
  const messages = [
    user("a", entry("manual", 1)),
    agent(""),
    user("c", entry("manual", 1)),
    agent("d"),
  ];
  const anchors = compactionSlots(messages).map((s) => s.anchor);
  assert.equal(new Set(anchors).size, anchors.length);
});

test("placeSlots puts a row between a user message and its answer, or after the last block", () => {
  const blocks = [
    { key: "k0", user: 0, last: 1 },
    { key: "k2", user: 2, last: 3 },
  ];
  const slot = (after: number) => ({ entry: entry("threshold", 1), anchor: after, after });
  const placed = placeSlots([slot(2), slot(9)], blocks);
  assert.deepEqual(
    placed.get("k2")!.map((p) => p.place),
    ["afterUser", "after"],
  );
  assert.equal(placed.has("k0"), false);
});

test("a /compact row after a tool-only answer shows after that answer", () => {
  const messages: AgentMessage[] = [
    user("a"),
    agent("b"),
    user("c", entry("manual", 1)),
    {
      role: "assistant",
      content: "",
      toolCalls: [{ id: "t1", tool: "read", reason: "", status: "done", startedAt: 0, endedAt: 1 }],
    },
  ];
  const blocks = [
    { key: "p0", user: 0, last: 1 },
    { key: "p1", user: 2, last: 3 },
  ];
  const placed = placeSlots(compactionSlots(messages), blocks);
  assert.deepEqual(
    placed.get("p1")!.map((p) => p.place),
    ["after"],
  );
});
