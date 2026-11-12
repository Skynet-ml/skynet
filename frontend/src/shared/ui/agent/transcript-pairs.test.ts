import assert from "node:assert/strict";
import { test } from "node:test";

import { pairMessages } from "./transcript-pairs.ts";

const user = (content: string, hidden = false) => ({ role: "user" as const, content, hidden });
const agent = (content: string) => ({ role: "assistant" as const, content });

test("replies after hidden turns join the block of the user's message", () => {
  const pairs = pairMessages([
    user("set up a run"),
    agent("a"),
    user("next screen", true),
    agent("b"),
    user("next screen", true),
    agent("c"),
  ]);
  assert.equal(pairs.length, 1);
  assert.equal(pairs[0]?.user?.msg.content, "set up a run");
  assert.equal(pairs[0]?.agent?.content, "c");
  assert.deepEqual(
    pairs[0]?.earlier.map((m) => m.content),
    ["a", "b"],
  );
});

test("a visible user message starts a new block", () => {
  const pairs = pairMessages([user("one"), agent("a"), user("two"), agent("b")]);
  assert.deepEqual(
    pairs.map((p) => [p.user?.msg.content, p.agent?.content, p.earlier.length]),
    [
      ["one", "a", 0],
      ["two", "b", 0],
    ],
  );
});

test("a hidden opening turn starts a block with no user bubble", () => {
  const pairs = pairMessages([user("hi", true), agent("a"), user("go", true), agent("b")]);
  assert.equal(pairs.length, 1);
  assert.equal(pairs[0]?.user, null);
  assert.deepEqual(
    pairs[0]?.earlier.map((m) => m.content),
    ["a"],
  );
});

test("a block keeps its key as replies join it", () => {
  const first = pairMessages([user("one"), agent("a")]);
  const later = pairMessages([user("one"), agent("a"), user("go", true), agent("b")]);
  assert.equal(later[0]?.key, first[0]?.key);
});

test("a user message with no reply yet is its own block", () => {
  const pairs = pairMessages([user("one"), agent("a"), user("two")]);
  assert.equal(pairs.length, 2);
  assert.equal(pairs[1]?.agent, null);
  assert.equal(pairs[1]?.user?.index, 2);
});
