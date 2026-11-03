import assert from "node:assert/strict";
import { test } from "node:test";

const { editMode, editTarget, retryMode } = await import("./edit-turn.ts");

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

test("a retry of an unconfirmed edit resends the edit target with regenerate", () => {
  const target = { text: "second", occurrence: 0 };
  assert.deepEqual(retryMode(target), { regenerate: true, editUserTurn: target });
});

test("a retry of a saved turn regenerates the latest stored turn", () => {
  assert.deepEqual(retryMode(null), { regenerate: true, editUserTurn: null });
});

// The chat after an edit of "second" to "v2" that failed before the server saved it.
const unsavedChat = [
  { role: "user", content: "first" },
  { role: "assistant", content: "r" },
  { role: "user", content: "v2" },
  { role: "assistant", content: "" },
];
const pending = { text: "second", occurrence: 0 };

test("a new edit of an unconfirmed edit targets the stored old turn with regenerate", () => {
  assert.deepEqual(editMode(unsavedChat, 2, pending), { regenerate: true, editUserTurn: pending });
});

test("a new edit of an earlier turn ignores the unconfirmed edit", () => {
  assert.deepEqual(editMode(unsavedChat, 0, pending), {
    regenerate: false,
    editUserTurn: { text: "first", occurrence: 0 },
  });
});

test("an edit with no unconfirmed edit targets the displayed turn", () => {
  assert.deepEqual(editMode(unsavedChat, 2, null), {
    regenerate: false,
    editUserTurn: { text: "v2", occurrence: 0 },
  });
});
