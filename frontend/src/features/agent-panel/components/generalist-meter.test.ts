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

// Each `cond && name` in an attribute whose right side is a plain name, such as a menu node.
function guardedNames(attribute: ts.Expression): Array<{ cond: string; name: string }> {
  const found: Array<{ cond: string; name: string }> = [];
  const visit = (node: ts.Node) => {
    if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken &&
      ts.isIdentifier(node.right)
    )
      found.push({ cond: node.left.getText(tree), name: node.right.text });
    ts.forEachChild(node, visit);
  };
  visit(attribute);
  return found;
}

// The slot's one guarded name is the model menu, hidden by `!<setup flag>`.
const slotGuards = guardedNames(composerAttribute("modelMenu"));
assert.equal(slotGuards.length, 1, "one guarded node in the slot");
const [{ cond: slotCond, name: menuName }] = slotGuards;
assert.match(slotCond, /^![A-Za-z_]\w*$/, "the slot hides the menu under a negated flag");
const setupFlag = slotCond.slice(1);

const messages = [{ role: "user", content: "hi" }];
const meterProps: unknown[] = [];

// The panel's `modelMenu` slot, run without mounting the panel around it.
function modelMenuSlot(inSetup: boolean): string {
  const code = ts.transpileModule(`(${composerAttribute("modelMenu").getText(tree)})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
  }).outputText;
  const context: Record<string, unknown> = {
    React,
    [setupFlag]: inSetup,
    agent: { messages },
    contextWindow: 100_000,
    streaming: true,
    ChatContextUsage: (props: unknown) => {
      meterProps.push(props);
      return React.createElement("span", { "data-meter": "" });
    },
    [menuName]: React.createElement("span", { "data-model-menu": "" }),
  };
  const slot = new Function(...Object.keys(context), `return ${code}`)(...Object.values(context));
  return renderToStaticMarkup(slot as React.ReactElement);
}

test("a chat outside the setup shows the context meter next to the model menu (SKY-736)", () => {
  meterProps.length = 0;
  assert.equal(modelMenuSlot(false), '<span data-meter=""></span><span data-model-menu=""></span>');
  assert.deepEqual(meterProps, [{ messages, contextWindow: 100_000, streaming: true }]);
  assert.equal((meterProps[0] as { messages: unknown }).messages, messages, "the chat's own list");
});

test("the setup chat keeps only the meter in the slot; its model menu sits in leadingControls", () => {
  meterProps.length = 0;
  assert.equal(modelMenuSlot(true), '<span data-meter=""></span>');
  assert.deepEqual(meterProps, [{ messages, contextWindow: 100_000, streaming: true }]);
  // The same menu shows on the start side under the opposite condition.
  const start = guardedNames(composerAttribute("leadingControls")).filter(
    (g) => g.name === menuName,
  );
  assert.deepEqual(start, [{ cond: setupFlag, name: menuName }]);
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

// The initializer of the panel's one `const <name> = ...`.
function declaration(name: string): string {
  const found: string[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === name && node.initializer)
      found.push(node.initializer.getText(tree));
    ts.forEachChild(node, visit);
  };
  visit(tree);
  assert.equal(found.length, 1, `one ${name}`);
  return found[0];
}

test("the draft originals are the shown originals of the draft's own files", () => {
  const names = ["draftFiles", "shownOriginals", "draftOriginals"];
  const code = ts.transpileModule(
    `${names.map((n) => `const ${n} = ${declaration(n)};`).join("\n")}\nreturn draftOriginals;`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } },
  ).outputText;
  const files = [{ name: "a.pdf", text: "" }];
  const asked: unknown[] = [];
  const context: Record<string, unknown> = {
    React: { useMemo: (make: () => unknown) => make() },
    draft: "the draft",
    splitPastes: (text: string) => ({ files: text === "the draft" ? files : [] }),
    chatMedia: {
      shownOriginals: (given: unknown) => {
        asked.push(given);
        return "shown";
      },
    },
  };
  const result = new Function(...Object.keys(context), code)(...Object.values(context));
  assert.equal(result, "shown");
  assert.deepEqual(asked, [files]);
  assert.equal(asked[0], files, "the same array as the one draft-files memo");
});
