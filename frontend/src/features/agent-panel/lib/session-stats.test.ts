import assert from "node:assert/strict";
import { test } from "node:test";

import { registerHooks } from "node:module";

import type { AgentMessage, ChatCompaction } from "@/shared/ui/agent/types";

// session-stats.ts imports its neighbour the way the app's bundler resolves
// it; the plain node runner needs the file path.
const statsUrl = new URL("./session-stats.ts", import.meta.url).href;
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (
      context.parentURL === statsUrl &&
      specifier.startsWith("./") &&
      !specifier.endsWith(".ts")
    ) {
      return { shortCircuit: true, url: new URL(`${specifier}.ts`, statsUrl).href };
    }
    return nextResolve(specifier, context);
  },
});

const { DEFAULT_CONTEXT_WINDOW, contextTokens, estimateTokens, sessionStats } = await import(
  "./session-stats.ts"
);

const compaction = (reason: "manual" | "threshold", cents: number): ChatCompaction => ({
  summary: "s".repeat(40),
  reason,
  summarizedTurns: 2,
  keptTurns: 1,
  tokensBefore: 0,
  tokensAfter: 0,
  cents,
  createdAt: "",
});
const stats = (cents: number, compactionCents?: number) =>
  ({ cents, compactionCents }) as AgentMessage["stats"];

test("session stats count visible messages, tool calls, compactions and cost", () => {
  const messages: AgentMessage[] = [
    { role: "user", content: "hi" },
    {
      role: "assistant",
      content: "ok",
      stats: stats(10),
      toolCalls: [
        { tool: "read", status: "done" },
        { tool: "submit", status: "done" },
      ] as AgentMessage["toolCalls"],
    },
    { role: "user", content: "auto", hidden: true, compaction: compaction("threshold", 4) },
    { role: "assistant", content: "", stats: stats(5, 4) },
    { role: "user", content: "more", compaction: compaction("manual", 2) },
  ];
  const s = sessionStats(messages, 200000);
  assert.equal(s.userMessages, 2);
  assert.equal(s.assistantMessages, 1);
  assert.equal(s.toolCalls, 1);
  assert.equal(s.compactions, 2);
  // Each compaction counts once: its turn's stats leave out its cost.
  assert.equal(s.cents, 17);
  assert.equal(s.contextWindow, 200000);
});

test("the context window falls back to the default", () => {
  assert.equal(sessionStats([], null).contextWindow, DEFAULT_CONTEXT_WINDOW);
  assert.equal(sessionStats([], 0).contextWindow, 128000);
});

test("context tokens count only the messages from the latest compaction on", () => {
  const empty = contextTokens([]);
  assert.equal(empty, 12000);
  const before: AgentMessage = { role: "user", content: "x".repeat(4000) };
  const kept: AgentMessage = {
    role: "user",
    content: "y".repeat(400),
    compaction: compaction("manual", 0),
  };
  // 400 characters of the kept turn plus the 40-character summary and its 160-character wrapper.
  assert.equal(contextTokens([before, kept]), 12000 + 150);
});

const toolTurn = (results: string[]): AgentMessage => ({
  role: "assistant",
  content: "ok",
  toolCalls: results.map((result) => ({
    tool: "read",
    status: "done",
    payload: { result },
  })) as AgentMessage["toolCalls"],
});

test("context tokens count each turn and tool result in full below the clearing trigger", () => {
  const messages: AgentMessage[] = [
    { role: "user", content: "x".repeat(8000) },
    toolTurn(["r".repeat(9000)]),
  ];
  const tools = JSON.stringify([
    { tool: "read", status: "done", result: JSON.stringify("r".repeat(9000)) },
  ]);
  assert.equal(contextTokens(messages, 1_000_000), 12000 + 2000 + 1 + Math.ceil(tools.length / 4));
});

const resultsChat = (count: number, chars = 4000): AgentMessage[] => [
  { role: "user", content: "go" },
  toolTurn(Array.from({ length: count }, (_, i) => `${i}`.padEnd(chars, "r"))),
];

test("at 40% of the window, the oldest batch of five results clears and the newest three stay", () => {
  const messages = resultsChat(12);
  const full = contextTokens(messages, 1_000_000);
  // The full count is just at the trigger: one batch of five clears.
  const window = Math.floor(full / 0.4);
  const cleared = contextTokens(messages, window);
  assert.ok(cleared < Math.floor(window * 0.4));
  // Five results cleared, seven kept in full: about 7 x 1000 tokens are left.
  assert.ok(cleared > 12000 + 7000 && cleared < 12000 + 7000 + 500, String(cleared));
  assert.equal(contextTokens(messages, window + 300), full);
});

test("a large window starts clearing at 60k tokens", () => {
  // Fourteen results of about 5000 tokens: about 82k tokens in full, far below 40% of the window.
  const cleared = contextTokens(resultsChat(14, 20_000), 1_000_000);
  assert.ok(cleared < 60_000, String(cleared));
  assert.ok(cleared > 12000 + 9 * 5000, String(cleared));
});

test("at 80% of the window, every older result clears but the newest three stay", () => {
  // Six results of 10000 tokens: no full batch, and the prompt still reaches the compaction.
  const cleared = contextTokens(resultsChat(6, 40_000), 20_000);
  assert.ok(cleared > 12000 + 3 * 10000 && cleared < 12000 + 3 * 10000 + 500, String(cleared));
});

test("fewer old results than one batch stay in full below the compaction", () => {
  const messages = resultsChat(7);
  const full = contextTokens(messages, 1_000_000);
  assert.equal(contextTokens(messages, Math.floor(full / 0.4)), full);
});

test("a compaction counts also when its turn stopped before done", () => {
  const messages: AgentMessage[] = [
    { role: "user", content: "hi", compaction: compaction("threshold", 4) },
    { role: "assistant", content: "" },
  ];
  assert.equal(sessionStats(messages, null).cents, 4);
});

test("side cents on a message add a summary cost that no entry holds", () => {
  const messages: AgentMessage[] = [
    { role: "user", content: "hi", sideCents: 3 },
    { role: "assistant", content: "ok", stats: { cents: 5 } as AgentMessage["stats"] },
  ];
  assert.equal(sessionStats(messages, null).cents, 8);
});

test("each non-ASCII character counts as one token", () => {
  assert.equal(estimateTokens("abcd"), 1);
  assert.equal(estimateTokens("שלום"), 4);
  assert.equal(estimateTokens("你好ab"), 3);
  assert.equal(estimateTokens("\u{1F600}"), 1);
});
