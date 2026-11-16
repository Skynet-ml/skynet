import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = "file-tiles.tsx";
const tree = ts.createSourceFile(
  source,
  readFileSync(new URL(`./${source}`, import.meta.url), "utf8"),
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

// The body of the module's `function <name>`.
function functionBody(name: string): ts.Block {
  let found: ts.Block | undefined;
  tree.forEachChild((node) => {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) found = node.body;
  });
  assert.ok(found, `function ${name}`);
  return found;
}

// Every node under `root` that `match` accepts.
function collect<T extends ts.Node>(root: ts.Node, match: (node: ts.Node) => node is T): T[] {
  const out: T[] = [];
  const visit = (node: ts.Node) => {
    if (match(node)) out.push(node);
    ts.forEachChild(node, visit);
  };
  visit(root);
  return out;
}

test("the draft viewer gives fileSlides the previews of one cache it makes on mount (SKY-736)", () => {
  const body = functionBody("SharedDraftViewer");
  const calls = collect(body, ts.isCallExpression);
  const caches = collect(body, ts.isVariableDeclaration).filter(
    (d) => d.initializer?.getText(tree) === "React.useState(previewCache)",
  );
  assert.equal(caches.length, 1, "one React.useState(previewCache)");
  const name = caches[0].name;
  assert.ok(ts.isArrayBindingPattern(name), "the cache comes out of the state pair");
  const cache = name.elements[0].getText(tree);
  const slides = calls.filter((c) => c.expression.getText(tree) === "fileSlides");
  assert.equal(slides.length, 1, "one fileSlides call");
  assert.deepEqual(
    slides[0].arguments.map((a) => a.getText(tree)),
    ["files", "originals", `${cache}(files)`],
  );
});
