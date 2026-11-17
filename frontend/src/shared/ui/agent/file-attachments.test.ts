/** Tests for attaching files of any type to a composer message. */

import assert from "node:assert/strict";
import test from "node:test";

import { MAX_ATTACHMENT_MB, describeAttachments, splitClaimed } from "./file-attachments.ts";
import { attachmentPath } from "./file-originals.ts";
import { withFilePaths } from "./pasted-text.ts";

test("any file type becomes a card with its type and size, in the order picked", () => {
  const files = [
    new File(["MZ"], "tool.exe"),
    new File(["%PDF"], "report.pdf", { type: "application/pdf" }),
    new File(["plain"], "README"),
  ];
  const result = describeAttachments(files);
  assert.deepEqual(
    result.attached.map((f) => f.name),
    ["tool.exe", "report.pdf", "README"],
  );
  assert.equal(result.attached[1].text, "type: application/pdf\nsize: 4 bytes");
  assert.equal(result.attached[2].text, "type: unknown\nsize: 5 bytes");
  assert.deepEqual(result.tooLarge, []);
});

test("oversized files are skipped", () => {
  const big = new File(["x"], "huge.bin");
  Object.defineProperty(big, "size", { value: MAX_ATTACHMENT_MB * 1024 * 1024 + 1 });
  const ok = new File(["b"], "ok.txt");
  const result = describeAttachments([big, ok]);
  assert.deepEqual(result.tooLarge, ["huge.bin"]);
  // Each file keeps its picked file, so its original can be stored (SKY-782).
  assert.deepEqual(
    result.attached.map((f) => [f.name, f.file]),
    [["ok.txt", ok]],
  );
});

test("Send puts each file's path in its card, and each card keeps its id", () => {
  const draft =
    'look\n\n<attached_file name="a.csv" id="c1">\ntype: text/csv\n</attached_file>\n\n' +
    '<attached_file name="a.csv" id="c2">\ntype: text/csv\n</attached_file>\n\n' +
    '<attached_file name="b.pdf" id="c3">\ntype: application/pdf\n</attached_file>';
  const sent = withFilePaths(draft, ["attachments/m1/a.csv", "attachments/m2/a.csv", null]);
  assert.equal(
    sent,
    'look\n\n<attached_file name="a.csv" id="c1">\npath: attachments/m1/a.csv\ntype: text/csv\n</attached_file>\n\n' +
      '<attached_file name="a.csv" id="c2">\npath: attachments/m2/a.csv\ntype: text/csv\n</attached_file>\n\n' +
      '<attached_file name="b.pdf" id="c3">\ntype: application/pdf\n</attached_file>',
  );
  assert.equal(withFilePaths("hi", ["x"]), "hi");
  const forked =
    '<attached_file name="a.csv">\npath: attachments/old/a.csv\ntype: text/csv\n</attached_file>';
  assert.equal(
    withFilePaths(forked, ["attachments/new/a.csv"]),
    '<attached_file name="a.csv">\npath: attachments/new/a.csv\ntype: text/csv\n</attached_file>',
  );
  assert.equal(
    withFilePaths(forked, []),
    '<attached_file name="a.csv">\ntype: text/csv\n</attached_file>',
  );
});

test("a file's path uses the stored name and never points at a folder", () => {
  assert.equal(attachmentPath("m1", "a b.csv"), "attachments/m1/a b.csv");
  assert.equal(attachmentPath("m1", ".."), "attachments/m1/file");
  assert.equal(attachmentPath("m1", ""), "attachments/m1/file");
});

test("the host claims only the first file it takes; the others are skipped", () => {
  const isCsv = (file: File) => file.name.endsWith(".csv");
  const [a, notes, b] = [
    new File(["a"], "a.csv"),
    new File(["n"], "notes.pdf"),
    new File(["b"], "b.csv"),
  ];
  const result = splitClaimed([a, notes, b], isCsv);
  assert.equal(result.claimed, a);
  assert.deepEqual(result.extra, [b]);
  assert.deepEqual(result.rest, [notes]);
  assert.deepEqual(splitClaimed([notes], isCsv), { claimed: null, extra: [], rest: [notes] });
});
