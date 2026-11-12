import assert from "node:assert/strict";
import { test } from "node:test";

import { wordDiff } from "./line-diff.ts";

test("a word diff marks the changed words and keeps the spaces", () => {
  const parts = wordDiff("Sort the tickets fast", "Sort all tickets fast");
  assert.deepEqual(parts, [
    { type: "same", text: "Sort " },
    { type: "del", text: "the" },
    { type: "add", text: "all" },
    { type: "same", text: " tickets fast" },
  ]);
});

test("a word diff rebuilds both texts", () => {
  const before = "a b  c\nd";
  const after = "a x  c\nd e";
  const parts = wordDiff(before, after);
  const join = (skip: string) => parts.filter((p) => p.type !== skip).map((p) => p.text).join("");
  assert.equal(join("add"), before);
  assert.equal(join("del"), after);
});
