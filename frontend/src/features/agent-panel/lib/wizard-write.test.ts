import assert from "node:assert/strict";
import { test } from "node:test";

import {
  fieldValueText,
  liveValues,
  lockedCodeFiles,
  wizardWriteChanges,
  writingFields,
} from "./wizard-write.ts";

const call = (status: "running" | "done", result?: unknown) => ({
  id: "1",
  tool: "update_wizard_state",
  reason: "",
  status,
  startedAt: 0,
  endedAt: null,
  payload: {
    arguments: { task_objective: "Sort tickets", screen_notes: [], metric_code: "def m(): ..." },
    ...(result ? { result } : {}),
  },
});

test("a write lists each field with what it held before", () => {
  const done = call("done", { previous: { task_objective: "Old goal", metric_code: null } });
  assert.deepEqual(wizardWriteChanges(done), [
    { field: "task_objective", before: "Old goal", after: "Sort tickets" },
    { field: "metric_code", before: null, after: "def m(): ..." },
  ]);
});

test("only a running write in the newest turn holds its fields", () => {
  const user = { role: "user" as const, content: "go" };
  const assistant = (status: "running" | "done") => ({
    role: "assistant" as const,
    content: "",
    toolCalls: [call(status)],
  });
  assert.deepEqual(writingFields([user, assistant("running")]), [
    "task_objective",
    "screen_notes",
    "metric_code",
  ]);
  assert.deepEqual(writingFields([user, assistant("done")]), []);
  assert.deepEqual(writingFields([assistant("running"), user]), []);
});

test("empty values read as empty and objects as JSON", () => {
  assert.equal(fieldValueText(null), null);
  assert.equal(fieldValueText(""), null);
  assert.equal(fieldValueText("x"), "x");
  assert.equal(fieldValueText({ a: 1 }), '{\n  "a": 1\n}');
  assert.equal(fieldValueText(false), "false");
});

test("the form's value when the write landed wins over the turn-start one", () => {
  const done = call("done", { previous: { task_objective: "Old goal", metric_code: "x = 1" } });
  const live = liveValues({ metric_code: "x = 2", other: 1 }, ["metric_code", "task_objective"]);
  assert.deepEqual(live, { metric_code: "x = 2", task_objective: null });
  assert.deepEqual(wizardWriteChanges(done, live), [
    { field: "task_objective", before: null, after: "Sort tickets" },
    { field: "metric_code", before: "x = 2", after: "def m(): ..." },
  ]);
});

test("a running turn on the code screens locks both files; otherwise only the ones being written", () => {
  assert.deepEqual(lockedCodeFiles(true, []), ["run.py", "metric.py"]);
  assert.deepEqual(lockedCodeFiles(false, ["metric_code"]), ["metric.py"]);
  assert.deepEqual(lockedCodeFiles(false, ["task_objective"]), []);
});
