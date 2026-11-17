/*
 * node --test cannot load .tsx, so this runs through jiti (file-tiles.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  fileExtension,
  fileIconKind,
  fileSlides,
  fileTilePreview,
  NAME_MAX,
  NAME_TAIL,
  previewCache,
  shortFileName,
} from "./file-tiles";

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

void describe("draft file previews", () => {
  void it("parse each file once while new but equal file arrays come in (SKY-736)", () => {
    const parsed: string[] = [];
    const previewsOf = previewCache((name, text) => {
      parsed.push(name);
      return fileTilePreview(name, text);
    });
    const draft = () => [
      { name: "rows.csv", text: "a,b\n1,2" },
      { name: "notes.txt", text: "hello" },
    ];
    const first = fileSlides(draft(), [], previewsOf(draft()));
    const again = fileSlides(draft(), [], previewsOf(draft()));
    assert.deepEqual(parsed, ["rows.csv", "notes.txt"]);
    assert.equal(again[0]?.preview, first[0]?.preview);
    assert.ok(first[0]?.preview, "the CSV has a preview");
  });

  void it("parse a file again when its text changes, and forget a file that leaves", () => {
    const parsed: string[] = [];
    const previewsOf = previewCache((name, text) => {
      parsed.push(`${name}:${text}`);
      return fileTilePreview(name, text);
    });
    previewsOf([{ name: "rows.csv", text: "a\n1" }]);
    previewsOf([{ name: "rows.csv", text: "a\n2" }]);
    previewsOf([]);
    previewsOf([{ name: "rows.csv", text: "a\n2" }]);
    assert.deepEqual(parsed, ["rows.csv:a\n1", "rows.csv:a\n2", "rows.csv:a\n2"]);
  });

  void it("parse two same-name files once each while new but equal arrays come in", () => {
    const parsed: string[] = [];
    const previewsOf = previewCache((name, text) => {
      parsed.push(text);
      return fileTilePreview(name, text);
    });
    const draft = () => [
      { name: "data.csv", text: "a\n1" },
      { name: "data.csv", text: "a\n2" },
    ];
    const first = previewsOf(draft());
    for (let press = 0; press < 5; press += 1) previewsOf(draft());
    const last = previewsOf(draft());
    assert.deepEqual(parsed, ["a\n1", "a\n2"]);
    assert.equal(last[0], first[0]);
    assert.equal(last[1], first[1]);
    assert.notEqual(first[0], first[1]);
  });

  void it("give the same slides as a fresh parse", () => {
    const files = [
      { name: "rows.csv", text: "a,b\n1,2" },
      { name: "blob.bin", text: "" },
    ];
    assert.deepEqual(fileSlides(files, [], previewCache()(files)), fileSlides(files, []));
  });
});

void describe("file slides with originals (SKY-782)", () => {
  const doc = (id: string, name: string, card?: string) => ({
    id,
    name,
    kind: "document" as const,
    url: "",
    ...(card ? { card } : {}),
  });

  void it("link each card to its own original by id, also for two files with one name", () => {
    const files = [
      { name: "report.pdf", text: "first", id: "c-1" },
      { name: "report.pdf", text: "second", id: "c-2" },
    ];
    // The second card's original comes first: the order does not matter.
    const slides = fileSlides(files, [
      doc("m-2", "report.pdf", "c-2"),
      doc("m-1", "report.pdf", "c-1"),
    ]);
    assert.equal(slides[0]?.original?.id, "m-1");
    assert.equal(slides[1]?.original?.id, "m-2");
    // A card that left takes its original with it; the other keeps its own.
    const left = fileSlides(
      [files[1]!],
      [doc("m-2", "report.pdf", "c-2"), doc("m-1", "report.pdf", "c-1")],
    );
    assert.equal(left[0]?.original?.id, "m-2");
  });

  void it("show the text page for a card with no original, or one the viewer does not draw", () => {
    const slides = fileSlides(
      [
        { name: "report.pdf", text: "first", id: "c-1" },
        { name: "rows.csv", text: "a,b\n1,2", id: "c-2" },
      ],
      [doc("m-1", "report.pdf", "c-9"), doc("m-2", "rows.csv", "c-2")],
    );
    assert.equal(slides[0]?.original, undefined);
    assert.equal(slides[1]?.original, undefined);
    assert.ok(slides[1]?.preview);
  });

  void it("link a card whose stored name the server cut", () => {
    const long = `${"a".repeat(300)}.pdf`;
    const slides = fileSlides(
      [{ name: long, text: "t", id: "c-1" }],
      [doc("m-1", `${"a".repeat(251)}.pdf`, "c-1")],
    );
    assert.equal(slides[0]?.original?.id, "m-1");
  });

  void it("pair a file sent before SKY-782 by name with an original that has no card", () => {
    const slides = fileSlides(
      [
        { name: "old.pdf", text: "old" },
        { name: "new.pdf", text: "new", id: "c-1" },
      ],
      [doc("m-1", "new.pdf", "c-1"), doc("m-0", "old.pdf")],
    );
    assert.equal(slides[0]?.original?.id, "m-0");
    assert.equal(slides[1]?.original?.id, "m-1");
  });
});
