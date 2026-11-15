/** Tests for pairing attached files with their stored originals (SKY-735). */

import assert from "node:assert/strict";
import test from "node:test";

import { keptOriginals, originalFormat, pairOriginals } from "./file-originals.ts";

test("only pdf, docx and xlsx keep an original", () => {
  assert.equal(originalFormat("Brief.PDF"), "pdf");
  assert.equal(originalFormat("plan.docx"), "docx");
  assert.equal(originalFormat("data.xlsx"), "xlsx");
  assert.equal(originalFormat("deck.pptx"), null);
  assert.equal(originalFormat("notes.txt"), null);
  assert.equal(originalFormat(".pdf"), null);
  assert.equal(originalFormat("README"), null);
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
});

test("a file with no original falls back to its text", () => {
  assert.deepEqual(pairOriginals([{ name: "old.pdf" }], []), [null]);
  assert.deepEqual(pairOriginals([{ name: "deck.pptx" }], [{ name: "deck.pptx" }]), [null]);
});

test("an original whose file left the message is dropped", () => {
  const kept = { name: "keep.docx" };
  assert.deepEqual(keptOriginals([{ name: "keep.docx" }], [{ name: "gone.pdf" }, kept]), [kept]);
});
