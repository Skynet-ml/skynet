import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

import { storedOptimizerChoice } from "../../../shared/types/unified-run.ts";

const file = "src/features/agent-panel/hooks/use-wizard-state.tsx";
const text = readFileSync(new URL(`../../../../${file}`, import.meta.url), "utf8");
const tree = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

// Run one of the hook file's top-level declarations without mounting React.
function declaration(name: string, context: Record<string, unknown>): any {
  let found: ts.Node | undefined;
  const visit = (node: ts.Node) => {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === name && node.initializer) found = node.initializer;
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) found = node;
    ts.forEachChild(node, visit);
  };
  visit(tree);
  assert.ok(found, name);
  const code = ts.transpileModule(`(${found.getText(tree).replace(/^export /, "")})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return new Function(...Object.keys(context), `return ${code}`)(...Object.values(context));
}

const isRecord = declaration("isRecord", {});
const extractWizardPatch = declaration("extractWizardPatch", {
  isRecord,
  isStringList: declaration("isStringList", {}),
  parseScreenNotes: declaration("parseScreenNotes", { isRecord }),
  rolesFields: () => null,
  DATA_MODES: declaration("DATA_MODES", {}),
  storedOptimizerChoice,
});

test("repository engines pass through and unknown ids are dropped", () => {
  assert.equal(extractWizardPatch({ optimizer_choice: "shinka_evolve" }).optimizer_choice, "shinka_evolve");
  assert.equal(extractWizardPatch({ optimizer_choice: "grid_search" }).optimizer_choice, undefined);
});

test("a stale artifact kind from an old agent reply is dropped", () => {
  for (const kind of ["repo", "dspy_program"]) {
    assert.deepEqual(extractWizardPatch({ artifact_kind: kind, repository: "acme/bot" }), { repository: "acme/bot" });
  }
});

test("a created repository keeps its created-by-Skynet mark", () => {
  const patch = extractWizardPatch({
    wizard_state: { repository: "acme/new-bot", repo_created_by_skynet: true },
  });
  assert.deepEqual(patch, { repository: "acme/new-bot", repo_created_by_skynet: true });
});
