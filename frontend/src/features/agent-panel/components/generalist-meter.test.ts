import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const panel = "GeneralistPanel.tsx";

// The panel's `modelMenu` slot, run without mounting the panel around it.
function modelMenuSlot(inSetup: boolean): string {
  const text = readFileSync(new URL(`./${panel}`, import.meta.url), "utf8");
  const tree = ts.createSourceFile(panel, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let found: ts.Expression | undefined;
  const visit = (node: ts.Node) => {
    if (ts.isJsxAttribute(node) && node.name.getText(tree) === "modelMenu") {
      const init = node.initializer;
      if (init && ts.isJsxExpression(init)) found = init.expression;
    }
    ts.forEachChild(node, visit);
  };
  visit(tree);
  assert.ok(found, "the Composer's modelMenu slot");
  const code = ts.transpileModule(`(${found.getText(tree)})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  const context: Record<string, unknown> = {
    React,
    inSetup,
    agent: { messages: [] },
    contextWindow: 100_000,
    streaming: false,
    ChatContextUsage: () => React.createElement("span", { "data-meter": "" }),
    modelMenuNode: React.createElement("span", { "data-model-menu": "" }),
  };
  const slot = new Function(...Object.keys(context), `return ${code}`)(...Object.values(context));
  return renderToStaticMarkup(slot as React.ReactElement);
}

test("a chat outside the setup shows the context meter next to the model menu (SKY-736)", () => {
  assert.equal(modelMenuSlot(false), '<span data-meter=""></span><span data-model-menu=""></span>');
});

test("the setup chat keeps only the meter in the slot; its model menu sits on the start side", () => {
  assert.equal(modelMenuSlot(true), '<span data-meter=""></span>');
  const text = readFileSync(new URL(`./${panel}`, import.meta.url), "utf8");
  assert.ok(
    text.includes("{inSetup && modelMenuNode}"),
    "the setup's model menu in leadingControls",
  );
});
