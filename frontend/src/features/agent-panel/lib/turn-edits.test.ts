import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const lineDiffUrl = new URL("../../../shared/lib/line-diff.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/lib/line-diff") return { shortCircuit: true, url: lineDiffUrl };
    if (specifier === "./wizard-write") {
      return { shortCircuit: true, url: new URL("./wizard-write.ts", import.meta.url).href };
    }
    return nextResolve(specifier, context);
  },
});

const { editTotals, latestTurnEdits, messageEdits } = await import("./turn-edits.ts");

const call = (id: string, wizard_state: Record<string, unknown>) => ({
  id,
  tool: "hand_off_to_coding_agent",
  reason: "",
  status: "done" as const,
  startedAt: 0,
  endedAt: 1,
  payload: { result: { wizard_state, summary: "" } },
});

test("a turn's edits run from the first write's before-state to the last write's text", () => {
  const message = {
    role: "assistant" as const,
    content: "",
    toolCalls: [
      call("a", { metric_code: "x = 1\n", optimize_toml: "version = 1\n" }),
      call("b", { metric_code: "x = 2\n" }),
    ],
  };
  const edits = messageEdits(message, {
    a: { metric_code: "x = 0\n", optimize_toml: null },
    b: { metric_code: "x = 1\n" },
  });
  assert.deepEqual(edits, [
    { path: "metric.py", before: "x = 0\n", after: "x = 2\n", added: 1, removed: 1 },
    { path: "optimize.toml", before: "", after: "version = 1\n", added: 1, removed: 0 },
  ]);
  assert.deepEqual(editTotals(edits), { added: 2, removed: 1 });
});

test("a file the draft did not hold diffs against the repository's text", () => {
  const repoToml = 'version = 1\n[metric]\nname = "acc"\n';
  const message = {
    role: "assistant" as const,
    content: "",
    toolCalls: [call("a", { optimize_toml: repoToml.replace("acc", "f1") })],
  };
  const writeBefore = { a: { optimize_toml: null } };
  const base = new Map([["optimize.toml", repoToml]]);
  assert.deepEqual(messageEdits(message, writeBefore, base), [
    {
      path: "optimize.toml",
      before: repoToml,
      after: repoToml.replace("acc", "f1"),
      added: 1,
      removed: 1,
    },
  ]);
  assert.equal(latestTurnEdits([message], writeBefore, base)[0]!.before, repoToml);
});

test("a final newline is not an added line", () => {
  const message = {
    role: "assistant" as const,
    content: "",
    toolCalls: [call("a", { metric_code: "a\nb\nc\n" }), call("b", { run_code: "a\nb\n" })],
  };
  const edits = messageEdits(message, { a: { metric_code: "" }, b: { run_code: "a\nb" } });
  assert.deepEqual(
    edits.map(({ path, added, removed }) => ({ path, added, removed })),
    [
      { path: "metric.py", added: 3, removed: 0 },
      { path: "run.py", added: 0, removed: 0 },
    ],
  );
});

test("a call with no recorded before-state and a write that changes nothing show no edit", () => {
  const message = {
    role: "assistant" as const,
    content: "",
    toolCalls: [call("a", { metric_code: "same" }), call("b", { metric_code: "new" })],
  };
  assert.deepEqual(messageEdits(message, { a: { metric_code: "same" } }), []);
});

test("the latest turn is the newest message that edited a file", () => {
  const older = {
    role: "assistant" as const,
    content: "",
    toolCalls: [call("a", { metric_code: "1" })],
  };
  const user = { role: "user" as const, content: "thanks" };
  const reply = { role: "assistant" as const, content: "done" };
  const edits = latestTurnEdits([older, user, reply], { a: { metric_code: "" } });
  assert.equal(edits.length, 1);
  assert.equal(edits[0]!.path, "metric.py");
  assert.deepEqual(latestTurnEdits([user, reply], {}), []);
});
