/** Tests for pairing attached files with their stored originals (SKY-735). */

import assert from "node:assert/strict";
import test from "node:test";

import {
  fitOriginals,
  keptOriginals,
  originalFormat,
  originalsToKeep,
  pairOriginals,
  pairedPlaces,
} from "./file-originals.ts";

test("the viewer draws only a pdf, docx or xlsx original", () => {
  assert.equal(originalFormat("Brief.PDF"), "pdf");
  assert.equal(originalFormat("plan.docx"), "docx");
  assert.equal(originalFormat("data.xlsx"), "xlsx");
  assert.equal(originalFormat("deck.pptx"), null);
  assert.equal(originalFormat("notes.txt"), null);
  assert.equal(originalFormat(".pdf"), null);
  assert.equal(originalFormat("README"), null);
  // The server cleans these names to ".pdf" and stores opaque bytes.
  assert.equal(originalFormat(" .pdf"), null);
  assert.equal(originalFormat('".pdf'), null);
});

test("each file gets the first unused original with its name", () => {
  const a = { name: "a.pdf", id: "1" };
  const b = { name: "a.pdf", id: "2" };
  const c = { name: "my_sheet.xlsx", id: "3" };
  const files = [
    { name: "a.pdf" },
    { name: "notes.txt" },
    { name: "a.pdf" },
    { name: "my sheet.xlsx" },
  ];
  assert.deepEqual(pairOriginals(files, [a, b, { name: "my sheet.xlsx", id: "3" }]), [
    a,
    null,
    b,
    { name: "my sheet.xlsx", id: "3" },
  ]);
  // A name the server cleaned differently still pairs.
  assert.deepEqual(
    pairOriginals([{ name: 'my"sheet.xlsx' }], [{ name: "mysheet.xlsx", id: "3" }]),
    [{ name: "mysheet.xlsx", id: "3" }],
  );
  assert.deepEqual(pairOriginals([{ name: "mysheet.xlsx" }], [c]), [c]);
  // The server keeps only the part after the last backslash or slash.
  const report = { name: "report.pdf", id: "4" };
  assert.deepEqual(pairOriginals([{ name: "Q3\\report.pdf" }], [report]), [report]);
  assert.deepEqual(pairOriginals([{ name: "Q3/report.pdf" }], [report]), [report]);
});

test("a file with no original falls back to its text", () => {
  assert.deepEqual(pairOriginals([{ name: "old.pdf" }], []), [null]);
  assert.deepEqual(pairOriginals([{ name: "deck.pptx" }], [{ name: "deck.pptx" }]), [null]);
});

test("an original whose file left the message is dropped", () => {
  const kept = { name: "keep.docx" };
  assert.deepEqual(keptOriginals([{ name: "keep.docx" }], [{ name: "gone.pdf" }, kept]), [kept]);
});

test("a name whose file and original counts differ keeps no original", () => {
  const first = { name: "report.pdf", id: "1" };
  const second = { name: "report.pdf", id: "2" };
  const other = { name: "plan.docx", id: "3" };
  // One of two same-name files left: no one can tell whose original stays.
  assert.deepEqual(
    keptOriginals([{ name: "report.pdf" }, { name: "plan.docx" }], [first, second, other]),
    [other],
  );
  // The first file's upload failed: the second original must not pair with the first file.
  assert.deepEqual(keptOriginals([{ name: "report.pdf" }, { name: "report.pdf" }], [second]), []);
  assert.deepEqual(
    keptOriginals([{ name: "report.pdf" }, { name: "report.pdf" }], [first, second]),
    [first, second],
  );
});

test("a name's originals fit in a message together or not at all", () => {
  const a1 = { name: "a.pdf" };
  const a2 = { name: "a.pdf" };
  const c = { name: "c.pdf" };
  assert.deepEqual(fitOriginals([a1, a2, c], 3), [a1, a2, c]);
  assert.deepEqual(fitOriginals([a1, a2, c], 2), [a1, a2]);
  assert.deepEqual(fitOriginals([a1, c, a2], 1), [c]);
  assert.deepEqual(fitOriginals([a1, a2, c], 0), []);
  assert.deepEqual(fitOriginals([a1, a2, c], -1), []);
});

test("an original the viewer draws fits before one it does not draw (SKY-782)", () => {
  const csv = { name: "rows.csv" };
  const txt = { name: "notes.txt" };
  const pdf = { name: "brief.pdf" };
  assert.deepEqual(fitOriginals([csv, txt, pdf], 1), [pdf]);
  assert.deepEqual(fitOriginals([csv, txt, pdf], 2), [csv, pdf]);
});

test("every read file keeps its original when its text went in (SKY-782)", () => {
  const files = [
    { name: "a.pdf" },
    { name: "b.csv" },
    { name: "big.xlsx" },
    { name: "bad.docx" },
    { name: "c.docx" },
  ];
  assert.deepEqual(
    originalsToKeep(files, ["big.xlsx", "bad.docx"]).map((f) => f.name),
    ["a.pdf", "b.csv", "c.docx"],
  );
});

test("a file the viewer does not draw keeps its original in the message (SKY-782)", () => {
  const csv = { name: "rows.csv", id: "1" };
  const bin = { name: "README", id: "2" };
  assert.deepEqual(keptOriginals([{ name: "rows.csv" }, { name: "README" }], [csv, bin]), [
    csv,
    bin,
  ]);
  assert.deepEqual(keptOriginals([], [csv]), []);
});

test("pairedPlaces gives each leaving file's place among the files of its key (SKY-782)", () => {
  const files = [
    { name: "a<1>.pdf" },
    { name: "b.pdf" },
    { name: "a_1_.pdf" },
    { name: "a_1_.pdf" },
  ];
  assert.deepEqual(
    [...pairedPlaces(files, new Set([1, 3]))],
    [
      ["b.pdf", [0]],
      ["a1.pdf", [2]],
    ],
  );
  assert.deepEqual([...pairedPlaces(files, new Set())], []);
});
