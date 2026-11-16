import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const panel = "GeneralistPanel.tsx";
const tree = ts.createSourceFile(
  panel,
  readFileSync(new URL(`./${panel}`, import.meta.url), "utf8"),
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

// The expression a JSX attribute of the panel's one Composer holds.
function composerAttribute(name: string): ts.Expression {
  let found: ts.Expression | undefined;
  const visit = (node: ts.Node) => {
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) &&
      node.tagName.getText(tree) === "Composer"
    ) {
      for (const attr of node.attributes.properties)
        if (ts.isJsxAttribute(attr) && attr.name.getText(tree) === name) {
          const init = attr.initializer;
          if (init && ts.isJsxExpression(init)) found = init.expression;
        }
    }
    ts.forEachChild(node, visit);
  };
  visit(tree);
  assert.ok(found, `the Composer's ${name}`);
  return found;
}

// The panel's `modelMenu` slot, run without mounting the panel around it.
function modelMenuSlot(inSetup: boolean): string {
  const code = ts.transpileModule(`(${composerAttribute("modelMenu").getText(tree)})`, {
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

test("the setup chat keeps only the meter in the slot; its model menu sits in leadingControls", () => {
  assert.equal(modelMenuSlot(true), '<span data-meter=""></span>');
  let setupMenu = false;
  const visit = (node: ts.Node) => {
    if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken &&
      node.left.getText(tree) === "inSetup" &&
      node.right.getText(tree) === "modelMenuNode"
    )
      setupMenu = true;
    ts.forEachChild(node, visit);
  };
  visit(composerAttribute("leadingControls"));
  assert.ok(setupMenu, "leadingControls shows the model menu in the setup");
});

test("the composer gets the draft originals that Send sends, and none in a web chat", () => {
  const code = ts.transpileModule(`(${composerAttribute("originals").getText(tree)})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const pick = (webChat: boolean) =>
    new Function("webChat", "draftOriginals", `return ${code}`)(webChat, "draft");
  assert.equal(pick(false), "draft");
  assert.equal(pick(true), undefined);
});
