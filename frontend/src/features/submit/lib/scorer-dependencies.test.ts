import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import test from "node:test";

const srcUrl = new URL("../../../", import.meta.url).href;
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "./run-floor") {
      return nextResolve("./run-floor.ts", context);
    }
    if (specifier.startsWith("@/")) {
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    }
    return nextResolve(specifier, context);
  },
});
const { metricUsesModel, scorerCallsModel, withScorerImports } = await import(
  "./scorer-dependencies.ts"
);

test("ignores unrelated llm names", () => {
  assert.equal(scorerCallsModel("llm = 'judge'"), true);
  assert.equal(scorerCallsModel("output.llm"), false);
  assert.equal(scorerCallsModel('skynet.llm("judge")'), false);
  assert.equal(scorerCallsModel("def score(llm): return llm('x')"), true);
  assert.equal(scorerCallsModel("from other import foo as llm\nllm('x')"), true);
});

test("detects a rebound imported or injected helper", () => {
  for (const code of [
    "from skynet import llm\nllm = partial(llm)\nllm('x')",
    "llm = partial(llm)\nllm('x')",
    "from skynet import llm as judge\njudge = other\njudge('x')",
  ])
    assert.equal(scorerCallsModel(code), true);
});

test("detects direct, qualified and aliased model calls but ignores comments and strings", () => {
  for (const code of [
    'llm("judge")',
    'from skynet import llm as judge\njudge("x")',
    "from skynet import (\n    llm,\n)\n\ndef score(i, o, e):\n    return llm(o)",
    "import skynet\n\ndef score(i, o, e):\n    return skynet.llm(o)",
    'from skynet import llm\njudge = llm\njudge("x")',
  ])
    assert.equal(scorerCallsModel(code), true);
  assert.equal(scorerCallsModel('# llm("x")\ntext = "llm(x)"'), false);
});

test("makes legacy helpers explicit and keeps future imports first", () => {
  const code =
    '"""Scorer."""\nfrom __future__ import annotations\n\ndef score(c):\n    return llm(c), Image(c)\n';
  const updated = withScorerImports(code);
  assert.ok(
    updated.includes("from __future__ import annotations\n\nfrom skynet import llm, Image"),
  );
  assert.equal(withScorerImports(updated), updated);
});

test("preserves existing imports, qualified calls and locally defined helpers", () => {
  for (const code of [
    'from skynet import llm, Image\nllm("x")',
    'import skynet\nskynet.llm("x")',
    'def llm(x): return x\nllm("x")',
  ])
    assert.equal(withScorerImports(code), code);
});

test("prefers the contract check's uses_llm while it was run on the current metric code", () => {
  const code = "from skynet import llm\n\ndef score(i, o, e):\n    return 1.0";
  const snapshot = JSON.stringify({ metricCode: code, runCode: "x" });
  const state = (
    metricCode: string,
    uses_llm?: boolean,
    lastCheckSnapshot: string | null = snapshot,
  ) => ({
    workspace: {
      metricCode,
      lastCheck: uses_llm === undefined ? null : { uses_llm },
      lastCheckSnapshot,
    },
  });
  assert.equal(metricUsesModel(state(code)), true);
  assert.equal(metricUsesModel(state(code, false)), false);
  assert.equal(
    metricUsesModel(
      state(
        "def score(i, o, e):\n    return 1.0",
        true,
        JSON.stringify({ metricCode: "def score(i, o, e):\n    return 1.0" }),
      ),
    ),
    true,
  );
  assert.equal(metricUsesModel(state(`${code}\n`, false)), true);
  assert.equal(metricUsesModel(state(code, false, null)), true);
});
