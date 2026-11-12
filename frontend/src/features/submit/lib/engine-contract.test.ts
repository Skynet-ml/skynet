import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const unifiedRunUrl = new URL("../../../shared/types/unified-run.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/types/unified-run") return { shortCircuit: true, url: unifiedRunUrl };
    return nextResolve(specifier, context);
  },
});

const {
  AUTO_MIN_SCORER_RUNS,
  DEFAULT_PROPOSER,
  MAX_TOOL_CALLS_LIMITS,
  minScorerRuns,
  proposerKnobs,
  supportsIterationLimit,
  usesNativeProposer,
} = await import("./engine-contract.ts");
const { BLACKBOX_OPTIMIZERS } = await import(unifiedRunUrl);

test("iteration limits apply only to a version-capped engine", () => {
  for (const choice of ["meta_harness", "autosaddler", "shinka_evolve"] as const) {
    assert.equal(supportsIterationLimit(choice), true);
  }
  for (const choice of ["gepa", "best_of_n", "autoresearch", "auto"] as const) {
    assert.equal(supportsIterationLimit(choice), false);
  }
});

test("coding-agent proposers: Auto and the agent engines", () => {
  for (const choice of ["auto", "meta_harness", "autoresearch", "autosaddler"] as const) {
    assert.equal(usesNativeProposer(choice), true);
  }
  for (const choice of ["gepa", "best_of_n", "shinka_evolve"] as const) {
    assert.equal(usesNativeProposer(choice), false);
  }
});

test("black-box Auto's minimum budget covers five lanes plus the continuation", () => {
  assert.equal(AUTO_MIN_SCORER_RUNS, 6);
  assert.equal(minScorerRuns("auto"), AUTO_MIN_SCORER_RUNS);
  for (const choice of BLACKBOX_OPTIMIZERS) assert.equal(minScorerRuns(choice), 1);
});

test("proposer knobs follow the engine that reads them, and Auto exposes them all", () => {
  assert.deepEqual(proposerKnobs("meta_harness"), { candidates: true, ralph: false });
  assert.deepEqual(proposerKnobs("autoresearch"), { candidates: false, ralph: true });
  assert.deepEqual(proposerKnobs("autosaddler"), { candidates: false, ralph: false });
  assert.deepEqual(proposerKnobs("shinka_evolve"), { candidates: false, ralph: false });
  assert.deepEqual(proposerKnobs("auto"), { candidates: true, ralph: true });
});

test("the tool-call cap defaults to 100 within the backend range", () => {
  assert.equal(DEFAULT_PROPOSER.max_tool_calls, 100);
  assert.ok(DEFAULT_PROPOSER.max_tool_calls! >= MAX_TOOL_CALLS_LIMITS.min);
  assert.ok(DEFAULT_PROPOSER.max_tool_calls! <= MAX_TOOL_CALLS_LIMITS.max);
});
