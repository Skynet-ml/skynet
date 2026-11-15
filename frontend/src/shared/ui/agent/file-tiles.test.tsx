/*
 * node --test cannot load .tsx, so this runs through jiti (file-tiles.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fileExtension, fileIconKind, NAME_MAX, NAME_TAIL, shortFileName } from "./file-tiles";

void describe("file type icon", () => {
  void it("picks the icon from the extension, in any case", () => {
    assert.equal(fileIconKind("report.PDF"), "pdf");
    assert.equal(fileIconKind("rows.csv"), "sheet");
    assert.equal(fileIconKind("budget.xlsx"), "sheet");
    assert.equal(fileIconKind("notes.docx"), "doc");
    assert.equal(fileIconKind("main.py"), "code");
    assert.equal(fileIconKind("backup.tar.gz"), "archive");
  });

  void it("draws a plain file for an unknown or missing extension", () => {
    assert.equal(fileIconKind("blob.xyz"), "file");
    assert.equal(fileIconKind("Makefile"), "file");
    assert.equal(fileIconKind(".env"), "file");
    assert.equal(fileIconKind("trailing."), "file");
  });

  void it("reads the extension as the badge shows it", () => {
    assert.equal(fileExtension("Data.XLSX"), "xlsx");
    assert.equal(fileExtension("archive.tar.gz"), "gz");
    assert.equal(fileExtension(".gitignore"), "");
  });
});

void describe("file name truncation", () => {
  void it("keeps a short name whole", () => {
    const name = `${"a".repeat(NAME_MAX - 4)}.pdf`;
    assert.equal(shortFileName(name), name);
  });

  void it("cuts a long name in the middle and keeps its last 12 characters", () => {
    const name = `${"quarterly-revenue-".repeat(5)}final-v2.xlsx`;
    const short = shortFileName(name);
    assert.equal(Array.from(short).length, NAME_MAX);
    assert.ok(short.endsWith(`…${name.slice(-NAME_TAIL)}`));
    assert.ok(short.startsWith(name.slice(0, NAME_MAX - NAME_TAIL - 1)));
  });

  void it("never splits a character outside the basic plane", () => {
    const name = `${"📊".repeat(60)}.csv`;
    const short = shortFileName(name, 20);
    assert.equal(Array.from(short).length, 20);
    assert.ok(!short.includes("�"));
    assert.ok(short.endsWith("📊📊📊📊📊📊📊📊.csv"));
  });
});
