import assert from "node:assert/strict";
import { test } from "node:test";

import { chatToMarkdown } from "./chat-export.ts";

const chat = [
  { role: "user" as const, content: "Make the docs clearer" },
  {
    role: "assistant" as const,
    content: "I named the run.",
    plan: "Name the run, then ask for the doc.",
    model: "auto",
    servedModel: "gpt-5",
    stats: { inputTokens: 10, outputTokens: 4, cents: 25, durationMs: 1500, ttftMs: null },
    thinkingSegments: [
      { reasoning: "The run has no name.", startedAt: 1000, endedAt: 2000, toolsBefore: 0, summary: "Name first" },
      { reasoning: "Now ask for the doc.", startedAt: 3000, endedAt: 3500, toolsBefore: 1 },
    ],
    toolCalls: [
      {
        id: "a",
        tool: "update_wizard_state",
        reason: "Name the run",
        status: "done" as const,
        startedAt: 2000,
        endedAt: 3000,
        payload: { arguments: { patch: { job_name: "Docs" } }, result: { ok: true } },
      },
    ],
  },
  { role: "user" as const, content: "[screen kickoff]", hidden: true },
];

test("the messages copy holds only what the user and the agent said", () => {
  const out = chatToMarkdown(chat, "messages", "My run");
  assert.equal(
    out,
    ["# My run", "## You", "Make the docs clearer", "## Agent", "I named the run.", ""].join("\n\n").trimEnd() + "\n",
  );
});

test("the internals copy holds the plan, the thinking and each tool call in turn order", () => {
  const out = chatToMarkdown(chat, "internals");
  const order = out
    .split("\n")
    .filter((line) => line.startsWith("#"))
    .map((line) => line.split(" (")[0]);
  assert.deepEqual(order, [
    "# Skynet chat",
    "## You",
    "## Agent",
    "#### Plan",
    "#### Thinking",
    "#### Tool: update_wizard_state",
    "#### Thinking",
    "## You",
  ]);
  assert.match(out, /#### Thinking \(1s\) - Name first/);
  assert.match(out, /#### Tool: update_wizard_state \(done, 1s\)/);
  assert.match(out, /"job_name": "Docs"/);
  assert.match(out, /model gpt-5 \u00b7 in 10 tokens \u00b7 out 4 tokens \u00b7 cost 0\.25 USD \u00b7 took 1\.5s/);
  assert.match(out, /## You \(sent by the app\)/);
});

test("a result holding a fence gets a longer fence around it", () => {
  const out = chatToMarkdown(
    [
      {
        role: "assistant" as const,
        content: "",
        toolCalls: [
          {
            id: "a",
            tool: "read_file",
            reason: "",
            status: "done" as const,
            startedAt: 0,
            endedAt: null,
            payload: { result: "```json\n{}\n```" },
          },
        ],
      },
    ],
    "internals",
  );
  assert.match(out, /````json\n```json/);
});

test("a turn with nothing to show drops out", () => {
  const out = chatToMarkdown([{ role: "assistant", content: "   " }], "messages");
  assert.equal(out, "# Skynet chat\n");
});
