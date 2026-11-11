import assert from "node:assert/strict";
import { test } from "node:test";

const { checkFeedback, errorLine, setupCheckState } = await import("./setup-check.ts");

const TOML = [
  "version = 1",
  'instructions = "AGENTS.md"',
  "",
  "[paths]",
  'editable = ["src"]',
  "",
  '[target."planner"]',
  "command = 'make'",
  "[budget]",
  "editable = 3",
].join("\n");

test("the check state follows the run, the running flag and later edits", () => {
  const run = { text: "a", ok: false, errors: [{ section: null, key: null, message: "bad" }] };
  assert.equal(setupCheckState(null, false, "a"), "idle");
  assert.equal(setupCheckState(run, true, "a"), "running");
  assert.equal(setupCheckState(run, false, "a"), "failed");
  assert.equal(setupCheckState({ ...run, ok: true, errors: [] }, false, "a"), "passed");
  assert.equal(setupCheckState(run, false, "b"), "stale");
});

test("an error points at its key inside its own table", () => {
  assert.equal(errorLine(TOML, { section: "paths", key: "editable" }), 5);
  assert.equal(errorLine(TOML, { section: "budget", key: "editable" }), 10);
  assert.equal(errorLine(TOML, { section: null, key: "instructions" }), 2);
});

test("an error with no key, or a key the table lacks, points at the table header", () => {
  assert.equal(errorLine(TOML, { section: "target.planner", key: null }), 7);
  assert.equal(errorLine(TOML, { section: "paths", key: "protected" }), 4);
  assert.equal(errorLine(TOML, { section: "metric", key: "path" }), null);
});

test("the agent's message lists each error with its place", () => {
  const run = {
    text: TOML,
    ok: false,
    errors: [{ section: "paths", key: "editable", message: "paths.editable is empty" }],
  };
  assert.equal(
    checkFeedback(run, "Fix optimize.toml."),
    "Fix optimize.toml.\n- optimize.toml:5 [paths] editable: paths.editable is empty",
  );
});
