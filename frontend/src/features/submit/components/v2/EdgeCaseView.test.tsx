/*
 * node --test cannot load .tsx, so this runs through jiti (EdgeCaseView.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { EdgeCaseView } = await import("./EdgeCaseView");

function render(
  tree: unknown,
  patchDiff: string | null = null,
  whyMd: string | null = null,
): string {
  setClientLocale("en");
  return renderToStaticMarkup(<EdgeCaseView tree={tree} patchDiff={patchDiff} whyMd={whyMd} />);
}

const PATCH = "--- a/metric.py\n+++ b/metric.py\n@@ -1 +1 @@\n-return score\n+return 1.0";

void describe("EdgeCaseView", () => {
  void it("draws ScoreCompare with both scores left to right", () => {
    const html = render([
      { type: "ScoreCompare", exploit_score: 0.98, base_score: 0.41, exploit_label: "Cheat" },
    ]);
    assert.match(html, /data-edge-case="tree"/);
    assert.match(html, /Cheat/);
    // The UI catalog does not load under jiti, so default labels render as their keys.
    assert.match(html, /submit\.v2\.edge_case\.score\.base/);
    assert.match(html, /<dd dir="ltr"[^>]*>0\.98<\/dd>/);
    assert.match(html, /<dd dir="ltr"[^>]*>0\.41<\/dd>/);
  });

  void it("draws the translated default labels when ScoreCompare has none", () => {
    const html = render([{ type: "ScoreCompare", exploit_score: 1, base_score: 0 }]);
    assert.match(html, /<dt [^>]*>submit\.v2\.edge_case\.score\.exploit<\/dt>/);
    assert.match(html, /<dt [^>]*>submit\.v2\.edge_case\.score\.base<\/dt>/);
  });

  void it("keeps a near-perfect score apart from a perfect one", () => {
    const html = render([{ type: "ScoreCompare", exploit_score: 1, base_score: 0.99999 }]);
    assert.match(html, /<dd dir="ltr"[^>]*>1<\/dd>/);
    assert.match(html, /<dd dir="ltr"[^>]*>0\.99999<\/dd>/);
  });

  void it("draws a Diff in an LTR block with tinted lines", () => {
    const html = render([{ type: "Diff", diff: PATCH }]);
    assert.match(html, /<pre dir="ltr"/);
    assert.match(html, /patch\.diff/);
    assert.match(html, /bg-chart-2\/15[^>]*>\+return 1\.0</);
    assert.match(html, /bg-chart-5\/15[^>]*>-return score</);
  });

  void it("draws IOPair as three LTR blocks", () => {
    const html = render([{ type: "IOPair", input: "2+2", expected: "4", got: "five" }]);
    for (const key of ["input", "expected", "got"]) {
      assert.match(html, new RegExp(`submit\\.v2\\.edge_case\\.io\\.${key}`));
    }
    assert.equal(html.match(/<pre dir="ltr"/g)?.length, 3);
    assert.match(html, />five</);
  });

  void it("draws a Table with its header and rows", () => {
    const html = render([
      {
        type: "Table",
        columns: ["case", "score"],
        rows: [
          ["a", 1],
          ["b", 0.5],
          [null, true],
        ],
      },
    ]);
    assert.equal(html.match(/<th /g)?.length, 2);
    assert.equal(html.match(/<tr /g)?.length, 4);
    // Each cell isolates its own text, so the th and td keep the table's direction.
    assert.doesNotMatch(html, /<t[hd] [^>]*dir=/);
    assert.match(html, /<th [^>]*><bdi dir="auto">case<\/bdi><\/th>/);
    assert.match(html, /<td [^>]*><bdi dir="auto">a<\/bdi><\/td>/);
    assert.match(html, /<bdi dir="ltr">0\.5<\/bdi>/);
    // React drops booleans and null, so the view writes them as text.
    assert.match(html, /<bdi dir="ltr">true<\/bdi>/);
    assert.match(html, /<bdi dir="ltr"><\/bdi>/);
  });

  void it("draws each Callout tone with its own role", () => {
    assert.match(render([{ type: "Callout", tone: "info", text: "i" }]), /role="note"/);
    assert.match(render([{ type: "Callout", tone: "warn", text: "w" }]), /role="status"/);
    assert.match(render([{ type: "Callout", tone: "danger", text: "d" }]), /role="alert"/);
  });

  void it("draws a Chart frame with its label and point count", () => {
    for (const kind of ["bar", "line"]) {
      const html = render([{ type: "Chart", kind, title: "Scores", points: [{ x: 1, y: 2 }] }]);
      assert.match(html, /role="img"/);
      assert.match(html, /aria-label="submit\.v2\.edge_case\.chart"/);
      assert.match(html, />Scores</);
    }
  });

  void it("draws Text and Steps, with a child node inside a step", () => {
    const html = render([
      { type: "Text", text: "Plain words." },
      { type: "Steps", items: ["Read the label.", { type: "Diff", diff: "+x" }] },
    ]);
    assert.match(html, /<p [^>]*dir="auto"[^>]*>Plain words\.<\/p>/);
    assert.match(html, /<ol class="list-decimal[^"]*ps-5/);
    assert.equal(html.match(/<li>/g)?.length, 2);
    assert.match(html, /<li><bdi dir="auto">Read the label\.<\/bdi><\/li>/);
    assert.match(html, /<li><figure/);
  });

  void it("draws a nested Steps of strings inside a step", () => {
    const html = render([
      { type: "Steps", items: ["Outer.", { type: "Steps", items: ["Inner a.", "Inner b."] }] },
    ]);
    assert.equal(html.match(/<ol /g)?.length, 2);
    assert.equal(html.match(/<li>/g)?.length, 4);
    assert.match(html, /<li><bdi dir="auto">Inner b\.<\/bdi><\/li>/);
  });

  void it("falls back to patch.diff and why.md when the tree is invalid", () => {
    const html = render([{ type: "Unknown" }], PATCH, "The metric returns 1.0 for any answer.");
    assert.match(html, /data-edge-case="fallback"/);
    assert.match(html, /patch\.diff/);
    assert.match(html, /why\.md/);
    assert.match(html, /The metric returns 1\.0 for any answer\./);
  });

  void it("says the finding has no details when the fallback has nothing", () => {
    assert.match(render(null), /submit\.v2\.edge_case\.empty/);
  });

  void it("renders hostile strings as text, never as markup", () => {
    const script = '<script>alert(1)</script><img src=x onerror="alert(2)">';
    const html = render([
      { type: "Text", text: script },
      { type: "Callout", tone: "danger", text: script },
      { type: "Table", columns: [script], rows: [[script]] },
      { type: "Steps", items: [script] },
    ]);
    assert.doesNotMatch(html, /<script>/);
    assert.doesNotMatch(html, /<img/);
    assert.equal(html.match(/&lt;script&gt;alert\(1\)&lt;\/script&gt;/g)?.length, 5);
  });

  void it("falls back for a huge or deeply nested tree", () => {
    let deep: unknown = { type: "Text", text: "leaf" };
    for (let i = 0; i < 50; i += 1) deep = { type: "Steps", items: [deep] };
    assert.match(render([deep], PATCH), /data-edge-case="fallback"/);
    const rows = Array.from({ length: 10_000 }, () => ["x"]);
    assert.match(
      render([{ type: "Table", columns: ["a"], rows }], PATCH),
      /data-edge-case="fallback"/,
    );
  });

  void it("uses no raw HTML, eval or dynamic import", () => {
    const source = readFileSync(new URL("./EdgeCaseView.tsx", import.meta.url), "utf8");
    for (const banned of [/dangerouslySetInnerHTML/, /\beval\(/, /new Function/, /import\(/]) {
      assert.doesNotMatch(source, banned);
    }
  });
});
