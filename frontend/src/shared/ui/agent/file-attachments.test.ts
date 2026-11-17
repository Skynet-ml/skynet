/** Tests for reading files of any type into composer attachments. */

import assert from "node:assert/strict";
import test from "node:test";

import { MAX_ATTACHMENT_MB, readAttachments, splitClaimed } from "./file-attachments.ts";

test("any file type is sent for reading, in the order picked", async () => {
  const files = [
    new File(["MZ"], "tool.exe"),
    new File(["png"], "photo.png", { type: "image/png" }),
    new File(["%PDF"], "report.pdf", { type: "application/pdf" }),
    new File(["plain"], "README"),
  ];
  const seen: string[] = [];
  const result = await readAttachments(files, async (file) => {
    seen.push(file.name);
    return `text of ${file.name}`;
  });
  assert.deepEqual(seen, ["tool.exe", "photo.png", "report.pdf", "README"]);
  assert.deepEqual(
    result.attached.map((f) => f.name),
    ["tool.exe", "photo.png", "report.pdf", "README"],
  );
  assert.equal(result.attached[2].text, "text of report.pdf");
  assert.deepEqual(result.failed, []);
  assert.deepEqual(result.tooLarge, []);
});

test("oversized files are skipped and failed reads are reported", async () => {
  const big = new File(["x"], "huge.bin");
  Object.defineProperty(big, "size", { value: MAX_ATTACHMENT_MB * 1024 * 1024 + 1 });
  const ok = new File(["b"], "ok.txt");
  const result = await readAttachments([big, new File(["a"], "bad.docx"), ok], async (file) => {
    if (file.name === "bad.docx") throw new Error("500");
    return "ok";
  });
  assert.deepEqual(result.tooLarge, ["huge.bin"]);
  assert.deepEqual(result.failed, ["bad.docx"]);
  // Each read file keeps its picked file, so its original can be stored (SKY-782).
  assert.deepEqual(result.attached, [{ name: "ok.txt", text: "ok", file: ok }]);
});

test("files are read one at a time", async () => {
  let running = 0;
  let peak = 0;
  await readAttachments(
    [new File(["a"], "a.pdf"), new File(["b"], "b.pdf"), new File(["c"], "c.pdf")],
    async () => {
      running += 1;
      peak = Math.max(peak, running);
      await new Promise((resolve) => setTimeout(resolve, 5));
      running -= 1;
      return "ok";
    },
  );
  assert.equal(peak, 1);
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
