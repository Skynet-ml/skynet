import assert from "node:assert/strict";
import { test } from "node:test";

const { editTarget } = await import("./edit-turn.ts");

const chat = [
  { role: "user", content: "hi" },
  { role: "assistant", content: "hi" },
  { role: "user", content: "next" },
  { role: "assistant", content: "ok" },
  { role: "user", content: "hi" },
  { role: "assistant", content: "again" },
];

test("a first-time text is occurrence 0", () => {
  assert.deepEqual(editTarget(chat, 0), { text: "hi", occurrence: 0 });
  assert.deepEqual(editTarget(chat, 2), { text: "next", occurrence: 0 });
});

test("a repeated text counts only earlier user messages with that text", () => {
  assert.deepEqual(editTarget(chat, 4), { text: "hi", occurrence: 1 });
});
