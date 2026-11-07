import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

import type { OptimizerCatalogResponse, OptimizerOption } from "@/shared/types/unified-run";

const srcUrl = new URL("../../../", import.meta.url);

// The lib imports through the app's "@/..." alias and without extensions;
// map both onto the source files so node can load them directly.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/"))
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    if (specifier.startsWith(".") && !/\.[cm]?[jt]sx?$/.test(specifier) && context.parentURL)
      return { shortCircuit: true, url: new URL(`${specifier}.ts`, context.parentURL).href };
    return nextResolve(specifier, context);
  },
});

const { optimizerChoiceIssue, optimizerOptions } = await import("./optimizer-options.ts");

function option(id: OptimizerOption["id"], available = true, reason?: string): OptimizerOption {
  return {
    id,
    family: id === "auto" ? "auto" : id.startsWith("dspy_") ? "dspy" : "blackbox",
    available,
    unavailable_reason: reason ?? null,
    needs_reflection_model: true,
    uses_feedback: false,
    supports_iteration_limit: false,
    native_proposer: false,
  };
}

function catalog(
  options: OptimizerOption[],
  artifact_kind: OptimizerCatalogResponse["artifact_kind"] = "repo",
  data_mode: OptimizerCatalogResponse["data_mode"] = "pairs",
): OptimizerCatalogResponse {
  return {
    artifact_kind,
    data_mode,
    default: "auto",
    options,
    auto_resolves_to: [],
    sandbox_available: true,
  };
}

test("Auto leads and the list follows the setup's allowed optimizers", () => {
  const ids = optimizerOptions({
    kind: "dspy_program",
    mode: "pairs",
    catalog: null,
    guided: false,
    choice: "auto",
  }).map((view) => view.id);
  assert.deepEqual(ids, ["auto"]);
  const single = optimizerOptions({
    kind: "dspy_program",
    mode: "single",
    catalog: null,
    guided: false,
    choice: "auto",
  }).map((view) => view.id);
  assert.deepEqual(single, ["auto"]);
  const blackbox = optimizerOptions({
    kind: "repo",
    mode: "pairs",
    catalog: null,
    guided: false,
    choice: "auto",
  });
  assert.equal(blackbox[0].id, "auto");
  assert.equal(blackbox[0].family, "auto");
  assert.equal(blackbox.length, 7);
});

test("without a matching catalog every option is pending, not unavailable", () => {
  const stale = catalog([option("gepa", false, "no key")], "repo", "pairs");
  const views = optimizerOptions({
    kind: "dspy_program",
    mode: "pairs",
    catalog: stale,
    guided: false,
    choice: "auto",
  });
  assert.ok(views.every((view) => view.available && view.checking));
});

test("unavailable options show disabled with their reason", () => {
  const views = optimizerOptions({
    kind: "repo",
    mode: "pairs",
    catalog: catalog([option("gepa", false, "  sandbox down ")]),
    guided: false,
    choice: "gepa",
  });
  const gepa = views.find((view) => view.id === "gepa");
  assert.equal(gepa?.available, false);
  assert.equal(gepa?.unavailableReason, "sandbox down");
  // An option the catalog omits is not offered by the server.
  assert.equal(views.find((view) => view.id === "best_of_n")?.available, false);
});

test("guided hides unavailable options but keeps the current choice", () => {
  const cat = catalog([option("gepa", false), option("best_of_n", false), option("autoresearch")]);
  const views = optimizerOptions({
    kind: "repo",
    mode: "pairs",
    catalog: cat,
    guided: true,
    choice: "gepa",
  }).map((view) => view.id);
  assert.deepEqual(views, ["gepa", "autoresearch"]);
});

test("catalog entries outside the allowed list are dropped", () => {
  const views = optimizerOptions({
    kind: "dspy_program",
    mode: "single",
    catalog: catalog([option("auto"), option("dspy_gepa"), option("gepa")], "dspy_program", "single"),
    guided: false,
    choice: "auto",
  }).map((view) => view.id);
  assert.deepEqual(views, ["auto"]);
});

test("fallback flags come from the shared rules while the catalog loads", () => {
  const [auto] = optimizerOptions({
    kind: "dspy_program",
    mode: "pairs",
    catalog: null,
    guided: false,
    choice: "auto",
  });
  assert.equal(auto.family, "dspy");
  assert.equal(auto.usesFeedback, true);
  const repo = optimizerOptions({
    kind: "repo",
    mode: "pairs",
    catalog: null,
    guided: false,
    choice: "auto",
  });
  assert.equal(repo.find((view) => view.id === "gepa")?.usesFeedback, true);
  const meta = repo.find((view) => view.id === "meta_harness");
  assert.equal(meta?.nativeProposer, true);
  assert.equal(meta?.supportsIterationLimit, true);
});

test("a choice the setup no longer allows is reported, never reset", () => {
  const issue = optimizerChoiceIssue({
    kind: "dspy_program",
    mode: "single",
    catalog: null,
    choice: "dspy_miprov2",
  });
  assert.deepEqual(issue, { kind: "not_allowed" });
  assert.deepEqual(
    optimizerChoiceIssue({ kind: "repo", mode: "pairs", catalog: null, choice: "dspy_gepa" }),
    { kind: "not_allowed" },
  );
});

test("an unavailable choice reports the catalog's reason", () => {
  const cat = catalog([option("gepa"), option("shinka_evolve", false, "no GPU")]);
  assert.deepEqual(
    optimizerChoiceIssue({ kind: "repo", mode: "pairs", catalog: cat, choice: "shinka_evolve" }),
    { kind: "unavailable", reason: "no GPU" },
  );
  assert.equal(
    optimizerChoiceIssue({ kind: "repo", mode: "pairs", catalog: cat, choice: "gepa" }),
    null,
  );
});

test("a repository run offers Auto first, then the six repository engines", () => {
  const input = { kind: "repo", mode: "single", catalog: null, choice: "auto" } as const;
  const ids = optimizerOptions({ ...input, guided: false }).map((view) => view.id);
  assert.deepEqual(ids, [
    "auto",
    "gepa",
    "best_of_n",
    "autoresearch",
    "meta_harness",
    "autosaddler",
    "shinka_evolve",
  ]);
  assert.equal(optimizerChoiceIssue(input), null);
  assert.equal(optimizerChoiceIssue({ ...input, choice: "gepa" }), null);
  assert.deepEqual(optimizerChoiceIssue({ ...input, choice: "dspy_gepa" }), { kind: "not_allowed" });
});
