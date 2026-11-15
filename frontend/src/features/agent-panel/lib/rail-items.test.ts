import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

import type { AgentMessage } from "@/shared/ui/agent/types";

const lineCommentsUrl = new URL("../../../shared/ui/agent/line-comments.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/ui/agent/line-comments") {
      return { shortCircuit: true, url: lineCommentsUrl };
    }
    return nextResolve(specifier, context);
  },
});

const { outputsByToolCalls, railItemsOf } = await import("./rail-items.ts");
const { joinLineComments } = await import("../../../shared/ui/agent/line-comments.ts");

const user = (content: string): AgentMessage => ({ role: "user", content });
const reply = (content: string): AgentMessage => ({ role: "assistant", content });
const none = () => [];

test("each user message gets one item at its index", () => {
  const items = railItemsOf([user("first"), reply("a"), user("second"), reply("b")], none);
  assert.deepEqual(
    items.map((i) => i.index),
    [0, 2],
  );
});

test("the label is the first line, without the line comments", () => {
  const content = joinLineComments("Rename it\nand more", [{ path: "a.py", line: 2, text: "here" }]);
  const [item] = railItemsOf([user(content)], none);
  assert.equal(item?.label, "Rename it");
});

test("the preview is the first answer with text, cut at 400 characters", () => {
  const long = "x".repeat(500);
  const [item] = railItemsOf([user("q"), reply("  "), reply(long), reply("later")], none);
  assert.equal(item?.preview, "x".repeat(400));
});

test("a turn with no answer yet has no preview", () => {
  const [item] = railItemsOf([user("q")], none);
  assert.equal(item?.preview, null);
});

test("the outputs collect each changed file once, across the turn's replies", () => {
  const first = reply("one");
  const second = reply("two");
  const outputs = new Map([
    [first, ["a.py", "b.py"]],
    [second, ["b.py", "c.py"]],
  ]);
  const [item] = railItemsOf([user("q"), first, second], (m) => outputs.get(m) ?? []);
  assert.deepEqual(item?.outputs, ["a.py", "b.py", "c.py"]);
});

test("replies before the first user message are skipped", () => {
  assert.deepEqual(railItemsOf([reply("hello")], none), []);
});

test("a streamed token does not read a reply's files again", () => {
  let reads = 0;
  const outputsOf = outputsByToolCalls(() => {
    reads += 1;
    return ["a.py"];
  });
  const toolCalls = [{ id: "t1" }] as unknown as AgentMessage["toolCalls"];
  const first: AgentMessage = { role: "assistant", content: "Wri", toolCalls };
  // The stream spreads the message for each token and keeps its tool calls.
  const next: AgentMessage = { ...first, content: "Writing" };
  assert.deepEqual(outputsOf(first), ["a.py"]);
  assert.deepEqual(outputsOf(next), ["a.py"]);
  assert.equal(reads, 1);
  // New tool calls read the files again; a reply with none changed no files.
  outputsOf({ ...next, toolCalls: [...toolCalls!] });
  assert.equal(reads, 2);
  assert.deepEqual(outputsOf(reply("text")), []);
  assert.equal(reads, 2);
});

test("a hidden user message gets no item, and its reply adds to the last shown turn", () => {
  const hidden: AgentMessage = { role: "user", content: "internal kickoff", hidden: true };
  const first = reply("kickoff answer");
  const later = reply("back answer");
  const outputs = new Map([
    [first, ["a.py"]],
    [later, ["b.py"]],
  ]);
  const items = railItemsOf(
    [hidden, first, user("shown"), reply("ok"), { ...hidden, content: "back" }, later],
    (m) => outputs.get(m) ?? [],
  );
  assert.deepEqual(
    items.map((i) => [i.index, i.label, i.preview, i.outputs]),
    [[2, "shown", "ok", ["b.py"]]],
    "the reply before the first shown turn goes to no item",
  );
});
