import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const wizardWriteUrl = new URL("./wizard-write.ts", import.meta.url).href;
const lineDiffUrl = new URL("../../../shared/lib/line-diff.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (context.parentURL === wizardWriteUrl && specifier === "@/shared/lib/line-diff") {
      return { shortCircuit: true, url: lineDiffUrl };
    }
    return nextResolve(specifier, context);
  },
});

const {
  ROLES_WRITE_TOOL,
  fieldValueText,
  isLongText,
  isWizardWrite,
  listChange,
  liveValues,
  lockedCodeFiles,
  rolesFields,
  wizardWriteChanges,
  writeSummary,
  writingFields,
} = await import(wizardWriteUrl);

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

test("a field written with the value it already held is left out", () => {
  const done = call("done", {
    previous: { task_objective: "Sort tickets", screen_notes: null, metric_code: "def m(): ..." },
  });
  assert.deepEqual(wizardWriteChanges(done), []);
  const blank = call("done", { previous: { task_objective: "", metric_code: null } });
  assert.deepEqual(
    wizardWriteChanges({ ...blank, payload: { ...blank.payload, arguments: { task_objective: null } } }),
    [],
  );
});

test("the collapsed line names the fields and each code file's line counts", () => {
  assert.equal(
    writeSummary([
      { field: "task_objective", before: null, after: "Sort tickets" },
      { field: "metric_code", before: "a = 1\nb = 2\n", after: "a = 1\nb = 3\nc = 4\n" },
    ]),
    "task_objective, metric.py +2 −1",
  );
  assert.equal(writeSummary([]), "");
});

test("the collapsed line names each field in the given words", () => {
  const label = (field: string) => (field === "task_objective" ? "Objective" : field);
  assert.equal(
    writeSummary([{ field: "task_objective", before: null, after: "Sort tickets" }], label),
    "Objective",
  );
});

test("a list change keeps the new order and puts dropped items last", () => {
  assert.deepEqual(listChange(["a", "b"], ["c", "a"]), [
    { item: "c", state: "add" },
    { item: "a", state: "same" },
    { item: "b", state: "del" },
  ]);
});

test("a line break or a long value reads as a passage", () => {
  assert.equal(isLongText("short"), false);
  assert.equal(isLongText("one\ntwo"), true);
  assert.equal(isLongText("x".repeat(61)), true);
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

const running = (tool: string, args: Record<string, unknown>) => ({
  role: "assistant" as const,
  content: "",
  toolCalls: [{ ...call("running"), tool, payload: { arguments: args } }],
});

test("a running budget write holds the form's budget field", () => {
  assert.deepEqual(writingFields([running("update_wizard_state", { spending_limit_usd: 5 })]), [
    "max_cost_cents",
  ]);
});

test("a running column-role write holds the roles card's fields", () => {
  const roles = { column_roles: { q: "input", a: "output" }, dataset_columns: ["q", "a"] };
  assert.deepEqual(writingFields([running(ROLES_WRITE_TOOL, roles)]), [
    "input_fields",
    "expected_field",
  ]);
  assert.equal(isWizardWrite(ROLES_WRITE_TOOL), true);
  assert.equal(isWizardWrite("list_models_for_agent"), false);
});

test("a column-role map reads as the inputs and the expected field", () => {
  assert.deepEqual(rolesFields({ ctx: "input", junk: "ignore", q: "input", a: "output", b: "output" }), {
    input_fields: ["ctx", "q"],
    expected_field: "a",
  });
  assert.equal(rolesFields({ q: "input" }), null);
  assert.equal(rolesFields({ a: "output" }), null);
  assert.equal(rolesFields(null), null);
});

test("a column-role write shows the roles card's old and new values", () => {
  const done = {
    ...call("done"),
    tool: ROLES_WRITE_TOOL,
    payload: { arguments: { column_roles: { q: "input", a: "output" }, dataset_columns: ["q", "a"] } },
  };
  assert.deepEqual(wizardWriteChanges(done, { input_fields: ["a"], expected_field: "q" }), [
    { field: "input_fields", before: ["a"], after: ["q"] },
    { field: "expected_field", before: "q", after: "a" },
  ]);
});
