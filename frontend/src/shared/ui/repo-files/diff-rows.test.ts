import { test } from "node:test";
import assert from "node:assert/strict";
import {
  countChanges,
  diffLines,
  diffRows,
  diffWords,
  foldRows,
  fullDiffRows,
  mergeLineSpans,
  numberRows,
  splitRows,
} from "./diff-rows.ts";

test("diffLines keeps identical texts as same lines", () => {
  assert.deepEqual(diffLines("a\nb", "a\nb"), [
    { text: "a", kind: "same" },
    { text: "b", kind: "same" },
  ]);
});

test("diffLines reports insertions, deletions and replacements in order", () => {
  assert.deepEqual(diffLines("a\nb\nc", "a\nx\nc\nd"), [
    { text: "a", kind: "same" },
    { text: "b", kind: "removed" },
    { text: "x", kind: "added" },
    { text: "c", kind: "same" },
    { text: "d", kind: "added" },
  ]);
});

test("diffLines treats an empty seed as all additions", () => {
  assert.deepEqual(diffLines("", "one\ntwo"), [
    { text: "", kind: "removed" },
    { text: "one", kind: "added" },
    { text: "two", kind: "added" },
  ]);
});

test("diffWords isolates the words that changed and keeps whitespace", () => {
  const [left, right] = diffWords("You are a helpful assistant.", "You are a concise assistant.");
  assert.deepEqual(left, [
    { text: "You are a ", changed: false },
    { text: "helpful", changed: true },
    { text: " assistant.", changed: false },
  ]);
  assert.deepEqual(right, [
    { text: "You are a ", changed: false },
    { text: "concise", changed: true },
    { text: " assistant.", changed: false },
  ]);
});

test("diffRows highlights words for balanced replacements and whole lines otherwise", () => {
  const rows = diffRows("keep\nold line here\nkeep", "keep\nnew line here\nkeep");
  assert.deepEqual(
    rows.map((r) => r.kind),
    ["same", "removed", "added", "same"],
  );
  assert.deepEqual(rows[1].segments, [
    { text: "old", changed: true },
    { text: " line here", changed: false },
  ]);
  assert.deepEqual(rows[2].segments, [
    { text: "new", changed: true },
    { text: " line here", changed: false },
  ]);

  const unbalanced = diffRows("one", "one\ntwo\nthree");
  assert.equal(
    unbalanced.every((r) => r.segments.every((s) => !s.changed)),
    true,
  );
  assert.deepEqual(countChanges(unbalanced), { added: 2, removed: 0 });
});

test("fullDiffRows keeps every unchanged line around the change", () => {
  const rows = fullDiffRows("1\n2\n3\n4\n5", "1\n2\nthree\n4\n5");
  assert.deepEqual(
    rows.map((r) => [r.kind, r.segments.map((s) => s.text).join("")]),
    [
      ["same", "1"],
      ["same", "2"],
      ["removed", "3"],
      ["added", "three"],
      ["same", "4"],
      ["same", "5"],
    ],
  );
});

test("fullDiffRows shows a new file as all added and a deleted one as all removed", () => {
  assert.deepEqual(
    fullDiffRows("", "a\nb").map((r) => r.kind),
    ["added", "added"],
  );
  assert.deepEqual(
    fullDiffRows("a\nb", "").map((r) => r.kind),
    ["removed", "removed"],
  );
});

const numbered = (n: number) => Array.from({ length: n }, (_, i) => `l${i + 1}`).join("\n");

test("foldRows keeps context around a change and folds the rest", () => {
  const before = numbered(20);
  const after = before.replace("l10\n", "L10\n");
  const folded = foldRows(numberRows(fullDiffRows(before, after)), 2);
  const gaps = folded.filter((r) => "gap" in r);
  assert.equal(gaps.length, 2);
  assert.deepEqual(
    gaps.map((g) => ("gap" in g ? g.hidden : 0)),
    [7, 8],
  );
  const shown = folded.filter((r) => !("gap" in r));
  assert.deepEqual(
    shown.map((r) => ("gap" in r ? "" : `${r.kind}:${r.oldLine ?? "-"}/${r.newLine ?? "-"}`)),
    ["same:8/8", "same:9/9", "removed:10/-", "added:-/10", "same:11/11", "same:12/12"],
  );
});

test("foldRows opens an expanded gap", () => {
  const before = numbered(12);
  const after = `${before}\nnew`;
  const rows = numberRows(fullDiffRows(before, after));
  const [gap] = foldRows(rows, 1);
  assert.ok(gap && "gap" in gap);
  const opened = new Set(rows.slice(gap.start, gap.start + gap.hidden).map((r) => r.oldLine!));
  assert.equal(foldRows(rows, 1, opened).length, rows.length);
});

test("foldRows keeps an opened gap open when a kept line splits it or a write moves its lines", () => {
  const before = numbered(40);
  const lines = before.split("\n");
  const after = ["first", ...lines.slice(1, 39), "last"].join("\n");
  const rows = numberRows(fullDiffRows(before, after));
  const gap = foldRows(rows, 3).find((r) => "gap" in r);
  assert.ok(gap && "gap" in gap);
  const opened = new Set(rows.slice(gap.start, gap.start + gap.hidden).map((r) => r.oldLine!));
  const gaps = (folded: ReturnType<typeof foldRows>) => folded.filter((r) => "gap" in r);
  assert.deepEqual(gaps(foldRows(rows, 3, opened, 4, new Set([20]))), []);
  // An agent write adds two lines at the top: every new line number moves down.
  const moved = numberRows(
    fullDiffRows(before, ["first", "x", "y", ...lines.slice(1, 39), "last"].join("\n")),
  );
  assert.deepEqual(gaps(foldRows(moved, 3, opened)), []);
});

test("foldRows returns a single gap for an unchanged file", () => {
  const rows = numberRows(fullDiffRows(numbered(10), numbered(10)));
  assert.deepEqual(foldRows(rows), [{ gap: true, start: 0, hidden: 10 }]);
});

test("foldRows leaves runs shorter than minFold unfolded", () => {
  const before = numbered(9);
  const after = before.replace("l5\n", "L5\n");
  const folded = foldRows(numberRows(fullDiffRows(before, after)), 1);
  assert.equal(folded.filter((r) => "gap" in r).length, 0);
  assert.equal(
    foldRows(numberRows(fullDiffRows(before, after)), 1, new Set(), 3).filter((r) => "gap" in r)
      .length,
    2,
  );
});

test("mergeLineSpans cuts tokens at change boundaries", () => {
  assert.deepEqual(
    mergeLineSpans(
      [
        { text: "return", spec: 1 },
        { text: " x + 1", spec: null },
      ],
      [
        { text: "retu", changed: false },
        { text: "rn x", changed: true },
        { text: " + 1", changed: false },
      ],
    ),
    [
      { text: "retu", spec: 1, changed: false },
      { text: "rn", spec: 1, changed: true },
      { text: " x", spec: null, changed: true },
      { text: " + 1", spec: null, changed: false },
    ],
  );
});

test("mergeLineSpans gives up when the texts differ", () => {
  assert.equal(mergeLineSpans([{ text: "a", spec: null }], [{ text: "b", changed: false }]), null);
});

const sideText = (row: { segments: { text: string }[] } | null) =>
  row ? row.segments.map((s) => s.text).join("") : null;

test("splitRows pairs removed and added lines and pads the shorter side", () => {
  const rows = splitRows(numberRows(fullDiffRows("a\nb\nc\nd", "a\nB\nd\ne\nf")));
  assert.deepEqual(
    rows.map((r) => ("gap" in r ? "gap" : [sideText(r.left), sideText(r.right)])),
    [
      ["a", "a"],
      ["b", "B"],
      ["c", null],
      ["d", "d"],
      [null, "e"],
      [null, "f"],
    ],
  );
});

test("splitRows keeps folded gaps between the pairs", () => {
  const before = Array.from({ length: 20 }, (_, i) => `l${i}`).join("\n");
  const after = before.replace("l10", "L10");
  const rows = splitRows(foldRows(numberRows(fullDiffRows(before, after)), 1));
  assert.deepEqual(
    rows.map((r) => ("gap" in r ? `gap ${r.hidden}` : `${r.left?.oldLine}|${r.right?.newLine}`)),
    ["gap 9", "10|10", "11|11", "12|12", "gap 8"],
  );
});

test("foldRows never folds a kept line, and folds the rest of its run around it", () => {
  const text = Array.from({ length: 20 }, (_, i) => `l${i + 1}`).join("\n");
  const rows = numberRows(fullDiffRows(text, text));
  const folded = foldRows(rows, 3, new Set(), 4, new Set([10]));
  assert.deepEqual(
    folded.map((r) => ("gap" in r ? `gap@${r.start}+${r.hidden}` : r.newLine)),
    ["gap@0+9", 10, "gap@10+10"],
  );
  assert.equal(foldRows(rows).length, 1);
});
