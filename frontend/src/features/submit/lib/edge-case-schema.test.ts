import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { describe, it } from "node:test";

import {
  MAX_CHART_POINTS,
  MAX_NODES,
  MAX_STRING,
  MAX_TABLE_ROWS,
  parseEdgeCase,
} from "./edge-case-schema.ts";

const ONE_OF_EACH = [
  {
    type: "ScoreCompare",
    exploit_score: 0.98,
    base_score: 0.41,
    exploit_label: "Cheat",
    base_label: "Honest",
  },
  {
    type: "Diff",
    diff: "--- a/metric.py\n+++ b/metric.py\n@@ -1 +1 @@\n-x\n+y",
    label: "metric.py",
  },
  { type: "IOPair", input: "2+2", expected: "4", got: "4 (copied from the label)" },
  {
    type: "Table",
    columns: ["case", "score"],
    rows: [
      ["a", 1],
      ["b", 0.5],
    ],
  },
  { type: "Callout", tone: "warn", text: "The metric reads the label." },
  {
    type: "Chart",
    kind: "bar",
    points: [
      { x: "base", y: 0.4 },
      { x: "exploit", y: 1 },
    ],
    label: "Scores",
  },
  { type: "Text", text: "Plain words." },
  { type: "Steps", items: ["Read the label.", { type: "Text", text: "Echo it." }] },
];

function text(value: string) {
  return [{ type: "Text", text: value }];
}

void describe("parseEdgeCase", () => {
  void it("keeps every node type with its fields", () => {
    assert.deepEqual(parseEdgeCase(ONE_OF_EACH), ONE_OF_EACH);
  });

  void it("accepts each node type alone, with optional labels left out", () => {
    for (const node of [
      { type: "ScoreCompare", exploit_score: 1, base_score: 0 },
      { type: "Diff", diff: "" },
      { type: "Callout", tone: "info", text: "i" },
      { type: "Callout", tone: "danger", text: "d" },
      { type: "Chart", kind: "line", points: [{ x: 1, y: 2 }] },
    ]) {
      assert.deepEqual(parseEdgeCase([node]), [node], node.type);
    }
  });

  void it("drops fields that the catalog does not know", () => {
    const tree = parseEdgeCase([
      { type: "Text", text: "a", html: "<b>a</b>", onClick: "alert(1)" },
    ]);
    assert.deepEqual(tree, text("a"));
  });

  void it("returns null for values that are not a non-empty node list", () => {
    for (const value of [null, undefined, 1, "x", {}, [], { nodes: [] }, [null], ["Text"]]) {
      assert.equal(parseEdgeCase(value), null, JSON.stringify(value));
    }
  });

  void it("returns null for an unknown node type, also inside Steps", () => {
    assert.equal(parseEdgeCase([{ type: "Script", text: "x" }]), null);
    assert.equal(parseEdgeCase([{ type: "text", text: "x" }]), null);
    assert.equal(parseEdgeCase([{ type: "Steps", items: [{ type: "Html", html: "x" }] }]), null);
  });

  void it("returns null for wrong field types", () => {
    for (const node of [
      { type: "ScoreCompare", exploit_score: "1", base_score: 0 },
      { type: "ScoreCompare", exploit_score: Number.NaN, base_score: 0 },
      { type: "ScoreCompare", exploit_score: Number.POSITIVE_INFINITY, base_score: 0 },
      { type: "Diff", diff: 1 },
      { type: "IOPair", input: "a", expected: "b" },
      { type: "Table", columns: [], rows: [] },
      { type: "Table", columns: ["a"], rows: [["x", "y"]] },
      { type: "Table", columns: ["a"], rows: [[{ v: 1 }]] },
      { type: "Callout", tone: "error", text: "x" },
      { type: "Chart", kind: "pie", points: [{ x: 1, y: 1 }] },
      { type: "Chart", kind: "bar", points: [] },
      { type: "Chart", kind: "bar", points: [{ x: 1, y: "1" }] },
      { type: "Text", text: { toString: "x" } },
      { type: "Steps", items: [] },
      { type: "Steps", items: [1] },
    ]) {
      assert.equal(parseEdgeCase([node]), null, JSON.stringify(node));
    }
  });

  void it("holds the node, row, point and string limits", () => {
    assert.equal(
      parseEdgeCase(Array.from({ length: MAX_NODES }, () => text("a")[0]))?.length,
      MAX_NODES,
    );
    assert.equal(parseEdgeCase(Array.from({ length: MAX_NODES + 1 }, () => text("a")[0])), null);

    const rows = (n: number) => Array.from({ length: n }, (_, i) => [i]);
    assert.ok(parseEdgeCase([{ type: "Table", columns: ["n"], rows: rows(MAX_TABLE_ROWS) }]));
    assert.equal(
      parseEdgeCase([{ type: "Table", columns: ["n"], rows: rows(MAX_TABLE_ROWS + 1) }]),
      null,
    );

    const points = (n: number) => Array.from({ length: n }, (_, i) => ({ x: i, y: i }));
    assert.ok(parseEdgeCase([{ type: "Chart", kind: "line", points: points(MAX_CHART_POINTS) }]));
    assert.equal(
      parseEdgeCase([{ type: "Chart", kind: "line", points: points(MAX_CHART_POINTS + 1) }]),
      null,
    );

    assert.ok(parseEdgeCase(text("x".repeat(MAX_STRING))));
    assert.equal(parseEdgeCase(text("x".repeat(MAX_STRING + 1))), null);
  });

  void it("counts nested nodes toward the node limit", () => {
    const steps = { type: "Steps", items: Array.from({ length: 20 }, () => text("a")[0]) };
    // 2 Steps nodes + 40 children = 42 nodes.
    assert.equal(parseEdgeCase([steps, steps]), null);
    // 1 Steps node + 20 children = 21 nodes.
    assert.ok(parseEdgeCase([steps]));
  });

  void it("allows depth 2 and rejects depth 3", () => {
    const inner = { type: "Steps", items: ["a"] };
    assert.ok(parseEdgeCase([{ type: "Steps", items: [inner] }]));
    assert.equal(
      parseEdgeCase([{ type: "Steps", items: [{ type: "Steps", items: [inner] }] }]),
      null,
    );
  });

  void it("keeps hostile strings as plain data", () => {
    const script = '<script>alert(1)</script><img src=x onerror="alert(2)">';
    assert.deepEqual(parseEdgeCase(text(script)), text(script));
  });

  void it("never throws on hostile values", () => {
    const getter = Object.defineProperty({ type: "Text" }, "text", {
      enumerable: true,
      get() {
        throw new Error("boom");
      },
    });
    const trap = new Proxy([], {
      get() {
        throw new Error("trap");
      },
    });
    let deep: unknown = { type: "Text", text: "leaf" };
    for (let i = 0; i < 10_000; i += 1) deep = { type: "Steps", items: [deep] };
    const huge = new Array(10_000_000);
    const cyclic: Record<string, unknown> = { type: "Steps" };
    cyclic.items = [cyclic];
    for (const value of [
      [getter],
      trap,
      [deep],
      huge,
      [{ type: "Table", columns: ["a"], rows: huge }],
      [{ type: "Chart", kind: "bar", points: huge }],
      [{ type: "Steps", items: huge }],
      [cyclic],
    ]) {
      assert.doesNotThrow(() => parseEdgeCase(value));
      assert.equal(parseEdgeCase(value), null);
    }
  });
});

const frontend = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../../..");

// node --test only loads .ts, so the EdgeCaseView .tsx render tests run in a jiti child.
void it("EdgeCaseView render tests pass", () => {
  const result = spawnSync(
    path.join(frontend, "node_modules/.bin/jiti"),
    ["src/features/submit/components/v2/EdgeCaseView.test.tsx"],
    {
      cwd: frontend,
      encoding: "utf8",
      env: {
        ...process.env,
        JITI_JSX: "1",
        JITI_ALIAS: JSON.stringify({ "@/": `${path.join(frontend, "src")}/` }),
      },
    },
  );
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});
