import assert from "node:assert/strict";
import { test } from "node:test";

import type { AgentMessage } from "@/shared/ui/agent/types";

const { modelChanges } = await import("./model-changes.ts");

const user = (content: string): AgentMessage => ({ role: "user", content });
const reply = (model?: string, servedModel?: string): AgentMessage => ({
  role: "assistant",
  content: "ok",
  model,
  servedModel,
});

test("one model for every turn shows no notice", () => {
  const changes = modelChanges([user("a"), reply("m1"), user("b"), reply("m1")]);
  assert.equal(changes.size, 0);
});

test("a new model marks the user message that starts its turn", () => {
  const changes = modelChanges([user("a"), reply("m1"), user("b"), reply("m2")]);
  assert.deepEqual([...changes], [[2, { from: "m1", to: "m2" }]]);
});

test("the served model stands in when the reply has no requested model", () => {
  const changes = modelChanges([user("a"), reply(undefined, "s1"), user("b"), reply("m2")]);
  assert.deepEqual([...changes], [[2, { from: "s1", to: "m2" }]]);
});

test("a turn with no model is skipped and does not reset the comparison", () => {
  const changes = modelChanges([
    user("a"),
    reply("m1"),
    user("b"),
    reply(),
    user("c"),
    reply("m2"),
  ]);
  assert.deepEqual([...changes], [[4, { from: "m1", to: "m2" }]]);
});

test("the first turn never shows a notice", () => {
  assert.equal(modelChanges([user("a"), reply("m1")]).size, 0);
});

test("a turn takes the model of its last reply", () => {
  const changes = modelChanges([user("a"), reply("m1"), user("b"), reply("m1"), reply("m2")]);
  assert.deepEqual([...changes], [[2, { from: "m1", to: "m2" }]]);
});

test("a change on a hidden user message's turn shows on the next shown user message", () => {
  const kickoff: AgentMessage = { role: "user", content: "next screen", hidden: true };
  const changes = modelChanges([user("a"), reply("m1"), kickoff, reply("m2"), user("b")]);
  assert.deepEqual([...changes], [[4, { from: "m1", to: "m2" }]]);
});

test("a hidden turn that changes the model and back shows no notice", () => {
  const kickoff: AgentMessage = { role: "user", content: "next screen", hidden: true };
  const changes = modelChanges([
    user("a"),
    reply("m1"),
    kickoff,
    reply("m2"),
    user("b"),
    reply("m1"),
  ]);
  assert.equal(changes.size, 0);
});
