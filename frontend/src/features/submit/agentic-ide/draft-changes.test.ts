import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const lineDiffUrl = new URL("../../../shared/lib/line-diff.ts", import.meta.url).href;
const diffRowsUrl = new URL("../../../shared/ui/repo-files/diff-rows.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/lib/line-diff") return { shortCircuit: true, url: lineDiffUrl };
    if (specifier === "@/shared/ui/repo-files/diff-rows") {
      return { shortCircuit: true, url: diffRowsUrl };
    }
    return nextResolve(specifier, context);
  },
});

const { draftRows, fileStats, fitDiffCache, isChanged, keepFiles, lineMap, replaceLine } =
  await import("./draft-changes.ts");

const lines = (n: number, from = 1) => Array.from({ length: n }, (_, i) => `line ${i + from}`);

test("a file's stats count its added and removed lines", () => {
  const files = [
    { path: "b.md", before: "x", after: "x" },
    { path: "a.py", before: "1", after: "2" },
  ];
  assert.deepEqual(fileStats(files[1]!), { added: 1, removed: 1 });
  assert.deepEqual(fileStats(files[0]!), { added: 0, removed: 0 });
});

test("a final newline is not a line: a new 3-line file counts +3", () => {
  assert.deepEqual(fileStats({ path: "a.py", before: "", after: "x\ny\nz\n" }), {
    added: 3,
    removed: 0,
  });
  assert.equal(draftRows("", "x\ny\nz\n").filter((row) => row.kind === "added").length, 3);
  const rows = draftRows("a\nb", "a\nb\n");
  assert.ok(rows.every((row) => row.kind === "same"));
  assert.deepEqual(fileStats({ path: "a.py", before: "a\nb", after: "a\nb\n" }), {
    added: 0,
    removed: 0,
  });
});

test("a change of only the final newline is not a change", () => {
  const file = { path: "metric.py", before: "x = 1", after: "x = 1\n" };
  assert.equal(isChanged(file), false);
  assert.deepEqual(fileStats(file), { added: 0, removed: 0 });
  assert.equal(isChanged({ path: "metric.py", before: "x = 1\n", after: "x = 2\n" }), true);
  assert.equal(isChanged({ path: "metric.py", before: "x", after: "x\n\n" }), true);
});

test("a file's line diff is computed once and reused while its texts stay the same", () => {
  const before = lines(50).join("\n");
  const after = before.replace("line 7", "line seven");
  const first = draftRows(before, after);
  assert.equal(draftRows(before, after), first);
  assert.notEqual(draftRows(before, `${after}\nmore`), first);
  // Many new files share the empty old text; each keeps its own diff.
  const news = Array.from({ length: 10 }, (_, i) => draftRows("", `new ${i}`));
  news.forEach((diff, i) => assert.equal(draftRows("", `new ${i}`), diff));
});

test("an unchanged file shows every line, numbered", () => {
  assert.deepEqual(
    draftRows("a\nb\n", "a\nb\n").map((r) => [r.kind, r.segments[0]?.text, r.oldLine, r.newLine]),
    [
      ["same", "a", 1, 1],
      ["same", "b", 2, 2],
    ],
  );
});

test("a line edit replaces, splits or deletes one line and keeps the final newline", () => {
  assert.equal(replaceLine("a\nb\nc\n", 2, "B"), "a\nB\nc\n");
  assert.equal(replaceLine("a\nb\nc", 2, "b1\nb2"), "a\nb1\nb2\nc");
  assert.equal(replaceLine("a\nb\nc\n", 3, null), "a\nb\n");
  assert.equal(replaceLine("a\n", 1, null), "");
  // Clearing the only line keeps an empty line, with its final newline.
  assert.equal(replaceLine("a\n", 1, ""), "\n");
  // A CRLF file keeps its line ends; the editor gives the line without its CR.
  assert.equal(replaceLine("a\r\nb\r\n", 1, "A"), "A\r\nb\r\n");
  assert.equal(replaceLine("a\r\nb\r\n", 1, "A1\nA2"), "A1\r\nA2\r\nb\r\n");
  assert.equal(replaceLine("a\r\nb\r\n", 1, null), "b\r\n");
  assert.equal(replaceLine("a\nb", 2, "B"), "a\nB");
  // With no final newline, the last line takes the file's line end, and no CR is left behind.
  assert.equal(replaceLine("a\r\nb", 2, null), "a");
  assert.equal(replaceLine("a\r\nb", 2, "b1\nb2"), "a\r\nb1\r\nb2");
  assert.equal(replaceLine("a\r\nb", 2, "B"), "a\r\nB");
});

test("the diff cache keeps every changed file of a large draft", () => {
  const files = Array.from({ length: 101 }, (_, i) => ({
    before: `cache ${i} old\n`,
    after: `cache ${i} new\n`,
  }));
  fitDiffCache(files.length);
  const first = files.map((f) => draftRows(f.before, f.after));
  const hits = files.filter((f, i) => draftRows(f.before, f.after) === first[i]).length;
  assert.equal(hits, 101);
});

test("a comment's line follows its line when lines above it move", () => {
  const text = `${lines(12).join("\n")}\n`;
  const to = lineMap(text, replaceLine(text, 3, null));
  assert.equal(to(10), 9);
  assert.equal(to(2), 2);
  // A removed line maps to the line now in its place.
  assert.equal(to(3), 3);
  assert.equal(lineMap(text, replaceLine(text, 1, "a\nb"))(10), 11);
  // A line past the new end stays on the last line.
  assert.equal(lineMap(text, "line 1\n")(10), 1);
});

test("a comment keeps its line in a file too large for a full diff", () => {
  const text = `${lines(2100).join("\n")}\n`;
  const edited = replaceLine(text, 5, "line 5 changed");
  assert.equal(lineMap(text, edited)(1500), 1500);
  assert.equal(lineMap(text, edited)(2000), 2000);
  assert.equal(lineMap(text, replaceLine(text, 5, "a\nb"))(1500), 1501);
  assert.equal(lineMap(text, replaceLine(text, 5, null))(1500), 1499);
  // A middle of one empty line is still a line.
  assert.equal(lineMap("a\n\nb\n", "a\nx\n\nb\n")(3), 4);
});

test("keepFiles keeps the object of each file whose texts did not change", () => {
  const a = { path: "a", before: "1", after: "2" };
  const b = { path: "b", before: "x", after: "y" };
  const prev = [a, b];
  assert.equal(keepFiles(prev, [{ ...a }, { ...b }]), prev);
  const next = keepFiles(prev, [{ ...a }, { ...b, after: "z" }]);
  assert.notEqual(next, prev);
  assert.equal(next[0], a);
  assert.deepEqual(next[1], { ...b, after: "z" });
  assert.deepEqual(keepFiles(prev, [{ ...b }]), [b]);
});
