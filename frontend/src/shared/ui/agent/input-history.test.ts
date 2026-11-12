/** Tests for the composer's Up/Down recall of sent messages. */

import assert from "node:assert/strict";
import test from "node:test";

import { HISTORY_IDLE, historyKeyDirection, sentHistory, stepHistory } from "./input-history.ts";
import type { AgentMessage } from "./types.ts";

const key = (k: string, mods: Partial<{ shiftKey: boolean; altKey: boolean; ctrlKey: boolean; metaKey: boolean }> = {}) => ({
  key: k,
  shiftKey: false,
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  ...mods,
});

const caret = (value: string, at: number, end = at) => ({ value, selectionStart: at, selectionEnd: end });

test("sentHistory keeps visible user messages, oldest first, without repeats in a row", () => {
  const messages: AgentMessage[] = [
    { role: "user", content: "kickoff", hidden: true },
    { role: "user", content: "first" },
    { role: "assistant", content: "reply" },
    { role: "user", content: "first" },
    { role: "user", content: "   " },
    { role: "user", content: "second" },
    { role: "user", content: "first" },
  ];
  assert.deepEqual(sentHistory(messages), ["first", "second", "first"]);
  assert.deepEqual(sentHistory(undefined), []);
});

test("historyKeyDirection: Up only on the first line, Down only on the last line", () => {
  assert.equal(historyKeyDirection(key("ArrowUp"), caret("", 0)), "older");
  assert.equal(historyKeyDirection(key("ArrowUp"), caret("one\ntwo", 2)), "older");
  assert.equal(historyKeyDirection(key("ArrowUp"), caret("one\ntwo", 5)), null);
  assert.equal(historyKeyDirection(key("ArrowDown"), caret("one\ntwo", 5)), "newer");
  assert.equal(historyKeyDirection(key("ArrowDown"), caret("one\ntwo", 1)), null);
  assert.equal(historyKeyDirection(key("ArrowDown"), caret("", 0)), "newer");
});

test("historyKeyDirection ignores modifiers, selections and other keys", () => {
  assert.equal(historyKeyDirection(key("ArrowUp", { shiftKey: true }), caret("", 0)), null);
  assert.equal(historyKeyDirection(key("ArrowUp", { metaKey: true }), caret("", 0)), null);
  assert.equal(historyKeyDirection(key("ArrowUp", { altKey: true }), caret("", 0)), null);
  assert.equal(historyKeyDirection(key("ArrowUp", { ctrlKey: true }), caret("", 0)), null);
  assert.equal(historyKeyDirection(key("ArrowUp"), caret("abc", 0, 2)), null);
  assert.equal(historyKeyDirection(key("ArrowLeft"), caret("", 0)), null);
});

test("stepHistory walks back newest first and forward to the saved draft", () => {
  const entries = ["a", "b", "c"];
  let nav = HISTORY_IDLE;
  let value = "my draft";
  const seen: string[] = [];
  for (let i = 0; i < 3; i++) {
    const step = stepHistory(entries, nav, value, "older");
    assert.ok(step);
    ({ nav, value } = step);
    seen.push(value);
  }
  assert.deepEqual(seen, ["c", "b", "a"]);
  assert.equal(stepHistory(entries, nav, value, "older"), null, "stops at the oldest entry");

  const forward: string[] = [];
  for (let i = 0; i < 3; i++) {
    const step = stepHistory(entries, nav, value, "newer");
    assert.ok(step);
    ({ nav, value } = step);
    forward.push(value);
  }
  assert.deepEqual(forward, ["b", "c", "my draft"]);
  assert.deepEqual(nav, HISTORY_IDLE);
  assert.equal(stepHistory(entries, nav, value, "newer"), null, "nothing newer than the draft");
});

test("stepHistory does nothing with no history", () => {
  assert.equal(stepHistory([], HISTORY_IDLE, "", "older"), null);
  assert.equal(stepHistory([], HISTORY_IDLE, "", "newer"), null);
});

test("an edited entry becomes the new draft", () => {
  const entries = ["a", "b"];
  const first = stepHistory(entries, HISTORY_IDLE, "", "older");
  assert.ok(first);
  assert.equal(first.value, "b");
  // The user edits the recalled "b", then browses again.
  const older = stepHistory(entries, first.nav, "b edited", "older");
  assert.ok(older);
  assert.equal(older.value, "b", "browsing restarts from the newest entry");
  const back = stepHistory(entries, older.nav, older.value, "newer");
  assert.ok(back);
  assert.equal(back.value, "b edited", "the edit is kept as the draft");
});

test("a sent message resets browsing", () => {
  const entries = ["a", "b"];
  const first = stepHistory(entries, HISTORY_IDLE, "", "older");
  assert.ok(first);
  // The recalled "b" was sent again (a repeat in a row adds no entry) and the composer is empty.
  const after = stepHistory(entries, first.nav, "", "older");
  assert.ok(after);
  assert.equal(after.value, "b");
  assert.equal(stepHistory(entries, first.nav, "", "newer"), null);
});

test("a message that arrives while browsing keeps the draft", () => {
  const first = stepHistory(["a", "b", "c"], HISTORY_IDLE, "my draft", "older");
  assert.ok(first);
  assert.equal(first.value, "c");
  // A queued follow-up auto-sends, so the transcript grows while "c" is shown.
  const grown = ["a", "b", "c", "d"];
  const newer = stepHistory(grown, first.nav, first.value, "newer");
  assert.ok(newer);
  assert.equal(newer.value, "d");
  const back = stepHistory(grown, newer.nav, newer.value, "newer");
  assert.ok(back);
  assert.equal(back.value, "my draft");
});
