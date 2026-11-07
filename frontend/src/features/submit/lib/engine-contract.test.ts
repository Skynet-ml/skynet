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
  submittedProposer,
  submittedRunProposer,
  supportsIterationLimit,
  usesNativeProposer,
} = await import("./engine-contract.ts");
const { DEFAULT_SHINKA_SETTINGS } = await import("./shinka-settings.ts");
const { BLACKBOX_OPTIMIZERS, DSPY_OPTIMIZERS } = await import(unifiedRunUrl);

test("iteration limits apply only to a version-capped engine", () => {
  for (const choice of ["meta_harness", "autosaddler", "shinka_evolve"] as const) {
    assert.equal(supportsIterationLimit(choice, "code"), true);
  }
  for (const choice of ["gepa", "best_of_n", "autoresearch", "auto"] as const) {
    assert.equal(supportsIterationLimit(choice, "repo"), false);
  }
  for (const choice of DSPY_OPTIMIZERS) {
    assert.equal(supportsIterationLimit(choice, "dspy_program"), false);
  }
  assert.equal(supportsIterationLimit("auto", "dspy_program"), false);
});

test("coding-agent proposers: black-box Auto and the agent engines, never DSPy", () => {
  for (const choice of ["auto", "meta_harness", "autoresearch", "autosaddler"] as const) {
    assert.equal(usesNativeProposer(choice, "code"), true);
  }
  for (const choice of ["gepa", "best_of_n", "shinka_evolve"] as const) {
    assert.equal(usesNativeProposer(choice, "code"), false);
  }
  for (const choice of DSPY_OPTIMIZERS) {
    assert.equal(usesNativeProposer(choice, "dspy_program"), false);
  }
  // Auto on a DSPy program resolves to DSPy GEPA, which calls its model directly.
  assert.equal(usesNativeProposer("auto", "dspy_program"), false);
});

test("black-box Auto's minimum budget covers five lanes plus the continuation", () => {
  assert.equal(AUTO_MIN_SCORER_RUNS, 6);
  assert.equal(minScorerRuns("auto", "repo"), AUTO_MIN_SCORER_RUNS);
  assert.equal(minScorerRuns("auto", "dspy_program"), 1);
  for (const choice of BLACKBOX_OPTIMIZERS) assert.equal(minScorerRuns(choice, "text"), 1);
});

test("proposer knobs follow the engine that reads them, and Auto exposes them all", () => {
  assert.deepEqual(proposerKnobs("meta_harness", "code"), { candidates: true, ralph: false });
  assert.deepEqual(proposerKnobs("autoresearch", "code"), { candidates: false, ralph: true });
  assert.deepEqual(proposerKnobs("autosaddler", "code"), { candidates: false, ralph: false });
  assert.deepEqual(proposerKnobs("shinka_evolve", "code"), { candidates: false, ralph: false });
  assert.deepEqual(proposerKnobs("auto", "repo"), { candidates: true, ralph: true });
  assert.deepEqual(proposerKnobs("auto", "dspy_program"), { candidates: false, ralph: false });
});

test("submitted proposer resets every knob the form hides, and always runs research rounds", () => {
  const tuned = {
    ...DEFAULT_PROPOSER,
    harness: "pi" as const,
    max_candidates_per_iter: 4,
    ralph: false,
    max_no_eval_seconds: 600,
  };
  const stall = DEFAULT_PROPOSER.max_no_eval_seconds;
  assert.deepEqual(submittedProposer(tuned, "auto", "repo"), {
    ...tuned,
    ralph: true,
    max_no_eval_seconds: stall,
  });
  assert.deepEqual(submittedProposer(tuned, "autosaddler", "code"), {
    ...tuned,
    max_candidates_per_iter: null,
    ralph: true,
    max_no_eval_seconds: null,
  });
  assert.deepEqual(submittedProposer(tuned, "meta_harness", "code"), {
    ...tuned,
    ralph: true,
    max_no_eval_seconds: null,
  });
  assert.deepEqual(submittedProposer(tuned, "autoresearch", "code"), {
    ...tuned,
    ralph: true,
    max_candidates_per_iter: null,
    max_no_eval_seconds: stall,
  });
});

test("the tool-call cap defaults to 100 within the backend range and is always submitted", () => {
  assert.equal(DEFAULT_PROPOSER.max_tool_calls, 100);
  assert.ok(DEFAULT_PROPOSER.max_tool_calls! >= MAX_TOOL_CALLS_LIMITS.min);
  assert.ok(DEFAULT_PROPOSER.max_tool_calls! <= MAX_TOOL_CALLS_LIMITS.max);
  for (const choice of ["meta_harness", "autoresearch", "autosaddler"] as const)
    assert.equal(
      submittedProposer({ ...DEFAULT_PROPOSER, max_tool_calls: 40 }, choice, "code").max_tool_calls,
      40,
    );
  assert.equal(
    submittedProposer({ ...DEFAULT_PROPOSER, max_tool_calls: null }, "auto", "code").max_tool_calls,
    100,
  );
});

test("ShinkaEvolve sends a proposer only for the agent editor's tool-call cap", () => {
  const proposer = { ...DEFAULT_PROPOSER, harness: "opencode" as const, max_tool_calls: 250 };
  const shinka = { ...DEFAULT_SHINKA_SETTINGS };
  const base = { proposer, choice: "shinka_evolve" as const, kind: "repo" as const };
  assert.equal(submittedRunProposer({ ...base, shinka }), undefined);
  assert.equal(submittedRunProposer({ ...base, shinka: null }), undefined);
  assert.deepEqual(submittedRunProposer({ ...base, shinka: { ...shinka, editor: "agent" } }), {
    harness: DEFAULT_PROPOSER.harness,
    max_tool_calls: 250,
  });
  // Optimizers without a coding agent never send one.
  for (const choice of ["gepa", "best_of_n"] as const)
    assert.equal(
      submittedRunProposer({ ...base, choice, shinka: { ...shinka, editor: "agent" } }),
      undefined,
    );
  assert.equal(
    submittedRunProposer({ proposer, choice: "auto", kind: "dspy_program", shinka: null }),
    undefined,
  );
  // A coding-agent proposer sends its own settings, cap included.
  assert.deepEqual(
    submittedRunProposer({ proposer, choice: "auto", kind: "repo", shinka }),
    submittedProposer(proposer, "auto", "repo"),
  );
});
