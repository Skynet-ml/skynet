import assert from "node:assert/strict";
import { test } from "node:test";

import type { AgentMessage, ChatCompaction } from "@/shared/ui/agent/types";
import {
  chatWindow,
  compactionAnchor,
  compactionSnippets,
  latestCompactionIndex,
  parseCompaction,
  withCompaction,
  withServerCost,
  withSideCents,
} from "./compaction.ts";

const entry = (summary: string): ChatCompaction => ({
  summary,
  reason: "threshold",
  summarizedTurns: 2,
  keptTurns: 2,
  tokensBefore: 100,
  tokensAfter: 40,
  cents: 3,
  createdAt: "",
});
const user = (content: string, extra: Partial<AgentMessage> = {}): AgentMessage => ({
  role: "user",
  content,
  ...extra,
});
const agent = (content: string, extra: Partial<AgentMessage> = {}): AgentMessage => ({
  role: "assistant",
  content,
  ...extra,
});

test("a chat with no compaction sends every message with text", () => {
  const sent = chatWindow([user("a"), agent(""), user("b")]);
  assert.deepEqual(sent.turns, [
    { role: "user", content: "a" },
    { role: "user", content: "b" },
  ]);
  assert.deepEqual(sent.indices, [0, 2]);
  assert.equal(sent.summary, null);
});

test("a chat sends the messages from the latest compaction on, with its summary", () => {
  const messages = [
    user("a"),
    agent("b"),
    user("c", { compaction: entry("old") }),
    agent("d"),
    user("e", { compaction: entry("new") }),
    agent("f"),
  ];
  assert.equal(latestCompactionIndex(messages), 4);
  const sent = chatWindow(messages);
  assert.deepEqual(sent.indices, [4, 5]);
  assert.equal(sent.summary, "new");
});

test("tool results in the history are sent in full and submit calls are left out", () => {
  const long = "x".repeat(5000);
  const sent = chatWindow([
    agent("done", {
      toolCalls: [
        { tool: "read", status: "done", payload: { result: long } },
        { tool: "submit", status: "done" },
      ] as AgentMessage["toolCalls"],
    }),
  ]);
  const calls = sent.turns[0]?.tool_calls ?? [];
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.result, JSON.stringify(long));
});

test("the anchor is the first kept turn, or the fallback when none was kept", () => {
  assert.equal(compactionAnchor([0, 2, 4, 5], 2, 6), 4);
  assert.equal(compactionAnchor([0, 2], 0, 6), 6);
  assert.equal(compactionAnchor([0, 2], 3, 6), null);
});

test("withCompaction stores the entry on one message and leaves the input as it is", () => {
  const messages = [user("a"), agent("b")];
  const next = withCompaction(messages, 0, entry("s"));
  assert.equal(next[0]?.compaction?.summary, "s");
  assert.equal(messages[0]?.compaction, undefined);
  assert.deepEqual(withCompaction(messages, null, entry("s")), messages);
  assert.deepEqual(withCompaction(messages, 9, entry("s")), messages);
});

test("parseCompaction reads a backend entry and rejects one with no summary", () => {
  assert.deepEqual(
    parseCompaction({
      summary: " s ",
      reason: "manual",
      summarized_turns: 4,
      kept_turns: 2,
      tokens_before: 9,
      tokens_after: 3,
      cents: 1.5,
      created_at: "t",
    }),
    {
      summary: "s",
      reason: "manual",
      summarizedTurns: 4,
      keptTurns: 2,
      tokensBefore: 9,
      tokensAfter: 3,
      cents: 1.5,
      createdAt: "t",
    },
  );
  assert.equal(parseCompaction({ summary: "  " }), null);
  assert.equal(parseCompaction(null), null);
  assert.equal(parseCompaction({ summary: "s" })?.reason, "threshold");
});

test("a turn sends each saved message id, so the backend stores a compaction on that row", () => {
  const sent = chatWindow([user("a", { id: 7 }), agent("b", { id: 8 }), user("c")]);
  assert.deepEqual(
    sent.turns.map((t) => t.id),
    [7, 8, undefined],
  );
  assert.equal("id" in sent.turns[2]!, false);
});

test("withSideCents adds to the named message, or to the latest one", () => {
  const messages = [user("a"), agent("b", { sideCents: 1 })];
  assert.equal(withSideCents(messages, 0, 2)[0]!.sideCents, 2);
  assert.equal(withSideCents(messages, null, 2)[1]!.sideCents, 3);
  assert.equal(withSideCents(messages, null, 0)[1]!.sideCents, 1);
  assert.equal(withSideCents(messages, 5, 2)[1]!.sideCents, 1);
  assert.equal(messages[1]!.sideCents, 1);
});

test("compaction snippets show both ends of a long window", () => {
  const messages = ["one", "two", "three", "four", "five", "six", "seven"].map((t) => user(t));
  assert.deepEqual(compactionSnippets(messages, null, 2), ["one…", "two…", "six…", "seven…"]);
  assert.deepEqual(compactionSnippets([user("a b c d e f g h")]), ["a b c d e f…"]);
});

test("compaction snippets come only from the turns the summary reads", () => {
  const messages = ["one", "two", "three", "kept", "new"].map((t) => user(t));
  assert.deepEqual(compactionSnippets(messages, 3), ["one…", "two…", "three…"]);
  assert.deepEqual(compactionSnippets(messages, 0), []);
});

test("the saved copy adds the cost of a summary call that a stop cut off", () => {
  const local: AgentMessage[] = [
    { role: "user", content: "q", id: 4 },
    { role: "assistant", content: "a" },
    { role: "user", content: "next" },
  ];
  const server: AgentMessage[] = [
    { role: "user", content: "q", id: 4, compaction: entry("s") },
    { role: "assistant", content: "a", id: 5, sideCents: 3 },
    { role: "user", content: "other", id: 6, sideCents: 9 },
  ];
  const merged = withServerCost(local, server);
  assert.deepEqual(merged[0]?.compaction, entry("s"));
  assert.equal(merged[1]?.sideCents, 3);
  assert.equal(merged[2], local[2]);
  assert.equal(merged[1]?.id, undefined);
});
