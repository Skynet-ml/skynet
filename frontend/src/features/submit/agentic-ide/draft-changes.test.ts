import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const lineDiffUrl = new URL("../../../shared/lib/line-diff.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/lib/line-diff") return { shortCircuit: true, url: lineDiffUrl };
    return nextResolve(specifier, context);
  },
});

const { diffRows, fileStats, hunkStarts, isChanged, orderFiles, revertHunk, splitRows } =
  await import("./draft-changes.ts");

const lines = (n: number, from = 1) => Array.from({ length: n }, (_, i) => `line ${i + from}`);

test("diff rows carry line numbers and fold the unchanged lines between hunks", () => {
  const before = lines(20).join("\n");
  const after = [...lines(1), "new top", ...lines(18, 2), "new end", ...lines(1, 20)]
    .filter((line) => line !== "line 19")
    .join("\n");
  const rows = diffRows(before, after, 1);
  assert.deepEqual(rows.slice(0, 3), [
    { type: "same", text: "line 1", oldNo: 1, newNo: 1 },
    { type: "add", text: "new top", newNo: 2, hunk: 0 },
    { type: "same", text: "line 2", oldNo: 2, newNo: 3 },
  ]);
  assert.deepEqual(rows[3], { type: "gap", count: 15 });
  assert.ok(rows.some((row) => row.type === "del" && row.text === "line 19" && row.hunk === 1));
});

test("reverting one hunk keeps the others", () => {
  const before = "a\nb\nc\nd\ne";
  const after = "a\nB\nc\nd\nE";
  assert.equal(revertHunk(before, after, 0), "a\nb\nc\nd\nE");
  assert.equal(revertHunk(before, after, 1), "a\nB\nc\nd\ne");
  assert.equal(revertHunk("", "x\ny", 0), "");
});

test("side by side pairs each removed line with the line that replaced it", () => {
  const split = splitRows(diffRows("a\nb\nc", "a\nB\nB2\nc"));
  assert.equal(split.length, 4);
  const pair = split[1]!;
  assert.equal(pair.type, "pair");
  if (pair.type === "pair") {
    assert.equal(pair.left?.text, "b");
    assert.equal(pair.right?.text, "B");
  }
  const lone = split[2]!;
  if (lone.type === "pair") {
    assert.equal(lone.left, null);
    assert.equal(lone.right?.text, "B2");
  }
});

test("the file tree lists changed files first and applies the filter", () => {
  const files = [
    { path: "b.md", before: "x", after: "x" },
    { path: "optimize.toml", before: "", after: "version = 1" },
    { path: "a.py", before: "1", after: "2" },
  ];
  assert.deepEqual(
    orderFiles(files, "").map((f) => f.path),
    ["a.py", "optimize.toml", "b.md"],
  );
  assert.deepEqual(
    orderFiles(files, "TOML").map((f) => f.path),
    ["optimize.toml"],
  );
  assert.deepEqual(fileStats(files[2]!), { added: 1, removed: 1 });
  assert.deepEqual(fileStats(files[0]!), { added: 0, removed: 0 });
});

test("unified and side-by-side views both give every hunk a revert row", () => {
  const before = lines(20).join("\n");
  const after = before.replace("line 3", "LINE 3").replace("line 15\n", "") + "\nline 21";
  const rows = diffRows(before, after, 1);
  const pairs = splitRows(rows);
  const unified = hunkStarts(rows);
  const split = hunkStarts(pairs);
  assert.deepEqual([...unified.values()], [0, 1, 2]);
  assert.deepEqual([...split.values()], [0, 1, 2]);
  for (const [idx, hunk] of split) {
    const row = pairs[idx]!;
    assert.equal(row.type, "pair");
    if (row.type === "pair") {
      const change = row.left?.type === "del" ? row.left : row.right;
      assert.ok(change && change.type !== "same" && change.hunk === hunk);
    }
  }
});

test("a final newline is not a line: a new 3-line file counts +3", () => {
  assert.deepEqual(fileStats({ path: "a.py", before: "", after: "x\ny\nz\n" }), {
    added: 3,
    removed: 0,
  });
  assert.equal(diffRows("", "x\ny\nz\n").filter((row) => row.type === "add").length, 3);
  const rows = diffRows("a\nb", "a\nb\n");
  assert.ok(!rows.some((row) => row.type === "add" || row.type === "del"));
  assert.deepEqual(fileStats({ path: "a.py", before: "a\nb", after: "a\nb\n" }), {
    added: 0,
    removed: 0,
  });
});

test("reverting a hunk keeps the new text's final newline", () => {
  assert.equal(revertHunk("a\nb\nc\n", "a\nB\nc\n", 0), "a\nb\nc\n");
  assert.equal(revertHunk("a\n", "a\nb\n", 0), "a\n");
  assert.equal(revertHunk("", "x\ny\n", 0), "");
});

test("a change of only the final newline is not a change", () => {
  const file = { path: "metric.py", before: "x = 1", after: "x = 1\n" };
  assert.equal(isChanged(file), false);
  assert.deepEqual(fileStats(file), { added: 0, removed: 0 });
  assert.equal(isChanged({ path: "metric.py", before: "x = 1\n", after: "x = 2\n" }), true);
  assert.equal(isChanged({ path: "metric.py", before: "x", after: "x\n\n" }), true);
  const order = orderFiles([file, { path: "z.py", before: "a", after: "b" }], "").map(
    (f) => f.path,
  );
  assert.deepEqual(order, ["z.py", "metric.py"]);
});
