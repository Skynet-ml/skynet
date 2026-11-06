import assert from "node:assert/strict";
import { test } from "node:test";

import type { AgentMessage } from "@/shared/ui/agent/types";

import { askedChoices } from "./ask-user.ts";

const options = [
  { label: "Exact match", description: "Score 1 only for the same text." },
  { label: "Judge model", description: "" },
];
const asked = (status: "done" | "running" | "error", result: unknown): AgentMessage => ({
  role: "assistant",
  content: "Which measure?",
  toolCalls: [
    {
      id: "1",
      tool: "ask_user",
      reason: "",
      status,
      startedAt: 0,
      endedAt: 1,
      payload: { result },
    },
  ],
});

test("askedChoices returns the options of the question that ends the chat", () => {
  assert.deepEqual(
    askedChoices([{ role: "user", content: "hi" }, asked("done", { question: "Which?", options })]),
    options,
  );
});

test("askedChoices hides once the user replied or while the call runs", () => {
  assert.equal(
    askedChoices([asked("done", { options }), { role: "user", content: "Exact match" }]),
    null,
  );
  assert.equal(askedChoices([asked("running", { options })]), null);
  assert.equal(askedChoices([]), null);
});

test("askedChoices ignores a refused call and malformed options", () => {
  assert.equal(askedChoices([asked("done", "Not shown: send one question")]), null);
  assert.equal(
    askedChoices([asked("done", { options: [{ label: "Only one" }, { label: " " }, 3] })]),
    null,
  );
  assert.deepEqual(
    askedChoices([asked("done", { options: [{ label: " A " }, { label: "B", description: 4 }] })]),
    [
      { label: "A", description: "" },
      { label: "B", description: "" },
    ],
  );
});
