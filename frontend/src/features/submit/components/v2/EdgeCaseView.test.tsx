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
    assert.match(html, /<dd class="[^"]*"><bdi dir="ltr">0\.98<\/bdi><\/dd>/);
    assert.match(html, /<dd class="[^"]*"><bdi dir="ltr">0\.41<\/bdi><\/dd>/);
  });

  void it("draws the translated default labels when ScoreCompare has none", () => {
    const html = render([{ type: "ScoreCompare", exploit_score: 1, base_score: 0 }]);
    assert.match(html, /<dt [^>]*>submit\.v2\.edge_case\.score\.exploit<\/dt>/);
    assert.match(html, /<dt [^>]*>submit\.v2\.edge_case\.score\.base<\/dt>/);
  });

  void it("keeps a near-perfect score apart from a perfect one", () => {
    const html = render([{ type: "ScoreCompare", exploit_score: 1, base_score: 0.99999 }]);
    assert.match(html, /<dd class="[^"]*"><bdi dir="ltr">1<\/bdi><\/dd>/);
    assert.match(html, /<dd class="[^"]*"><bdi dir="ltr">0\.99999<\/bdi><\/dd>/);
    const close = render([{ type: "ScoreCompare", exploit_score: 1, base_score: 0.9999999 }]);
    assert.match(close, /<bdi dir="ltr">0\.9999999<\/bdi>/);
    assert.equal(close.match(/<bdi dir="ltr">1<\/bdi>/g)?.length, 1);
  });

  void it("draws a Diff in an LTR block with tinted lines and no file name", () => {
    const html = render([{ type: "Diff", diff: PATCH }]);
    assert.match(html, /<pre dir="ltr"/);
    // Only the fallback names patch.diff, so a model diff is not taken for the real patch.
    assert.doesNotMatch(html, /<figcaption/);
    assert.doesNotMatch(html, /patch\.diff/);
    assert.match(html, /bg-chart-2\/15[^>]*>\+return 1\.0</);
    assert.match(html, /bg-chart-5\/15[^>]*>-return score</);
  });

  void it("draws IOPair as three blocks that follow their own text direction", () => {
    const html = render([{ type: "IOPair", input: "2+2", expected: "4", got: "five" }]);
    for (const key of ["input", "expected", "got"]) {
      assert.match(html, new RegExp(`submit\\.v2\\.edge_case\\.io\\.${key}`));
    }
    assert.equal(html.match(/<pre dir="auto"/g)?.length, 3);
    assert.doesNotMatch(html, /<pre dir="ltr"/);
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
          ["line one\nline two", false],
          ["d", 1234567.89],
        ],
      },
    ]);
    assert.equal(html.match(/<th /g)?.length, 2);
    assert.equal(html.match(/<tr /g)?.length, 6);
    // Each cell isolates its own text, so the th and td keep the table's direction.
    assert.doesNotMatch(html, /<t[hd] [^>]*dir=/);
    assert.match(html, /<th [^>]*><bdi dir="auto">case<\/bdi><\/th>/);
    assert.match(html, /<td [^>]*><bdi dir="auto">a<\/bdi><\/td>/);
    // A long or multi-line cell wraps instead of being cut to one line with an ellipsis.
    for (const cell of html.match(/<t[hd] [^>]*>/g) ?? []) {
      assert.match(cell, /whitespace-pre-wrap/);
      assert.match(cell, /break-words/);
      assert.doesNotMatch(cell, /whitespace-nowrap|text-ellipsis/);
    }
    assert.match(html, /<bdi dir="auto">line one\nline two<\/bdi>/);
    // Numbers use the locale format; booleans use translated words; null is an empty cell.
    assert.match(html, /<bdi dir="ltr">0\.5<\/bdi>/);
    assert.match(html, /<bdi dir="ltr">1,234,567\.89<\/bdi>/);
    assert.match(html, /<bdi>submit\.v2\.edge_case\.bool\.true<\/bdi>/);
    assert.match(html, /<bdi>submit\.v2\.edge_case\.bool\.false<\/bdi>/);
    assert.doesNotMatch(html, />(true|false)</);
    assert.match(html, /<td [^>]*><\/td>/);
  });

  void it("draws every Callout tone as a note, never as a live region", () => {
    for (const tone of ["info", "warn", "danger"]) {
      const html = render([{ type: "Callout", tone, text: "a\nb" }]);
      assert.match(html, new RegExp(`<div role="note" data-tone="${tone}"`), tone);
      assert.doesNotMatch(html, /role="(alert|status)"|aria-live/, tone);
      // The text keeps its line break, as a Text node does.
      assert.match(html, /whitespace-pre-wrap[^>]*>a\nb<\/div>/, tone);
    }
  });

  void it("draws a Chart frame with its label and point count", () => {
    for (const kind of ["bar", "line"]) {
      const html = render([{ type: "Chart", kind, title: "Scores", points: [{ x: 1, y: 2 }] }]);
      assert.match(html, /role="img"/);
      assert.match(html, /aria-label="submit\.v2\.edge_case\.chart"/);
      assert.match(html, />Scores</);
    }
  });

  void it("lists the chart values for screen readers", () => {
    const html = render([
      {
        type: "Chart",
        kind: "bar",
        points: [
          { x: "base", y: 0.4 },
          { x: "exploit", y: 1234.5 },
        ],
      },
    ]);
    const table = html.match(/<table class="sr-only">.*<\/table>/)?.[0] ?? "";
    assert.match(table, /<bdi dir="auto">base<\/bdi><\/td><td><bdi dir="ltr">0\.4<\/bdi>/);
    assert.match(table, /<bdi dir="auto">exploit<\/bdi><\/td><td><bdi dir="ltr">1,234\.5<\/bdi>/);
  });

  void it("orders a line chart with numeric x values by x", () => {
    const html = render([
      {
        type: "Chart",
        kind: "line",
        points: [
          { x: 10, y: 1 },
          { x: 2, y: 3 },
        ],
      },
    ]);
    const xs = [...html.matchAll(/<tr><td><bdi dir="ltr">([^<]*)<\/bdi>/g)].map((m) => m[1]);
    assert.deepEqual(xs, ["2", "10"]);
  });

  void it("draws Text and Steps, with a child node inside a step", () => {
    const html = render([
      { type: "Text", text: "Plain words." },
      { type: "Steps", items: ["Read the label.", { type: "Diff", diff: "+x" }] },
    ]);
    assert.match(html, /<p [^>]*dir="auto"[^>]*>Plain words\.<\/p>/);
    assert.match(html, /<ol class="[^"]*list-decimal[^"]*ps-5/);
    assert.equal(html.match(/<li>/g)?.length, 2);
    assert.match(html, /<li><bdi dir="auto" class="[^"]*">Read the label\.<\/bdi><\/li>/);
    assert.match(html, /<li><figure/);
  });

  void it("draws a nested Steps of strings inside a step", () => {
    const html = render([
      { type: "Steps", items: ["Outer.", { type: "Steps", items: ["Inner a.", "Inner b."] }] },
    ]);
    assert.equal(html.match(/<ol /g)?.length, 2);
    assert.equal(html.match(/<li>/g)?.length, 4);
    assert.match(html, /<li><bdi dir="auto" class="[^"]*">Inner b\.<\/bdi><\/li>/);
  });

  void it("wraps a long multi-line step and keeps its line breaks", () => {
    const step = `${"x".repeat(500)}\nsecond line`;
    const html = render([{ type: "Steps", items: [step] }]);
    const bdi = html.match(/<li><bdi [^>]*>/)?.[0] ?? "";
    assert.match(bdi, /whitespace-pre-wrap/);
    assert.match(bdi, /break-words/);
    assert.ok(html.includes(`>${step}</bdi>`));
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
