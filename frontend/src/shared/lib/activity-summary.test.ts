import assert from "node:assert/strict";
import { test } from "node:test";

import { activityGroupOpen, joinSummary, summarizeActivity } from "./activity-summary.ts";
import { diffHunks, diffStats, lineDiff } from "./line-diff.ts";

test("a burst names each kind once in Codex's order, with how many ran", () => {
  const summary = summarizeActivity([
    { kind: "read", status: "done" },
    { kind: "edit", status: "done" },
    { kind: "read", status: "done" },
    { kind: "check", status: "done" },
  ]);
  assert.deepEqual(summary.parts, [
    { kind: "edit", count: 1 },
    { kind: "read", count: 2 },
    { kind: "check", count: 1 },
  ]);
  assert.equal(summary.lead, "edit");
  assert.equal(summary.running, false);
  assert.equal(summary.failed, false);
});

test("a burst reports a running step and a failed step", () => {
  const summary = summarizeActivity([
    { kind: "search", status: "error" },
    { kind: "read", status: "running" },
  ]);
  assert.equal(summary.lead, "search");
  assert.equal(summary.running, true);
  assert.equal(summary.failed, true);
});

test("an empty burst has no lead", () => {
  assert.deepEqual(summarizeActivity([]), { parts: [], lead: null, running: false, failed: false });
});

test("summary phrases join into one capitalized line", () => {
  assert.equal(joinSummary(["edited a file", "read results"], "en"), "Edited a file, read results");
  assert.equal(joinSummary(["ערך קובץ"], "he"), "ערך קובץ");
});

test("a line diff counts added and removed lines", () => {
  const lines = lineDiff("a\nb\nc", "a\nB\nc\nd");
  assert.deepEqual(diffStats(lines), { added: 2, removed: 1 });
  assert.deepEqual(lines, [
    { type: "same", text: "a" },
    { type: "del", text: "b" },
    { type: "add", text: "B" },
    { type: "same", text: "c" },
    { type: "add", text: "d" },
  ]);
});

test("a new file is all additions", () => {
  assert.deepEqual(diffStats(lineDiff("", "x\ny")), { added: 2, removed: 0 });
});

test("hunks keep context around changes and fold the rest", () => {
  const before = ["1", "2", "3", "4", "5", "6", "7", "8"].join("\n");
  const after = ["1", "2", "3", "4", "5", "6", "7", "X"].join("\n");
  const hunks = diffHunks(lineDiff(before, after), 1);
  assert.deepEqual(hunks, [
    { type: "gap", count: 6 },
    { type: "same", text: "7" },
    { type: "del", text: "8" },
    { type: "add", text: "X" },
  ]);
});

test("a finished burst in the agent panel stays open until the reader folds it", () => {
  const done = { live: false, running: false, stayOpen: true };
  assert.equal(activityGroupOpen(null, done), true);
  assert.equal(activityGroupOpen(false, done), false);
  assert.equal(activityGroupOpen(null, { ...done, stayOpen: false }), false);
  assert.equal(activityGroupOpen(null, { live: true, running: false, stayOpen: false }), true);
});
