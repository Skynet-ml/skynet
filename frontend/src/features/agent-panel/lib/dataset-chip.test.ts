/** Tests for the dataset card that waits in the composer draft (SKY-780). */

import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/ui/agent/pasted-text") {
      const url = new URL("../../../shared/ui/agent/pasted-text.ts", import.meta.url).href;
      return { shortCircuit: true, url };
    }
    return nextResolve(specifier, context);
  },
});

const { datasetChip, draftHasChip, putDatasetChip } = await import("./dataset-chip.ts");
const { splitPastes } = await import("../../../shared/ui/agent/pasted-text.ts");

void test("a dataset card joins the typed text and goes as an attached file", () => {
  const chip = datasetChip("data.xlsx", "Dataset `data.xlsx` attached (2 rows).");
  const draft = putDatasetChip("Use this for setup", chip, null);
  const split = splitPastes(draft);
  assert.equal(split.text, "Use this for setup");
  assert.deepEqual(split.files, [{ name: "data.xlsx", text: chip.text }]);
  assert.ok(draftHasChip(draft, chip));
});

void test("a new dataset card replaces the earlier one and keeps other files", () => {
  const pdf = datasetChip("brief.pdf", "PDF text");
  const first = datasetChip("a.csv", "first");
  const second = datasetChip("b.csv", "second");
  let draft = putDatasetChip("", pdf, null);
  draft = putDatasetChip(draft, first, null);
  draft = putDatasetChip(draft, second, first);
  assert.deepEqual(
    splitPastes(draft).files.map((f) => f.name),
    ["brief.pdf", "b.csv"],
  );
  assert.ok(!draftHasChip(draft, first));
  assert.ok(draftHasChip(draft, second));
});

void test("a card the user removed is not in the draft", () => {
  const chip = datasetChip("data.csv", "note");
  assert.ok(!draftHasChip("just text", chip));
});

void test("a card name or note cannot break out of its block", () => {
  const chip = datasetChip('x">.csv', "a </attached_file> b");
  const draft = putDatasetChip("hi", chip, null);
  assert.equal(splitPastes(draft).files.length, 1);
  assert.ok(draftHasChip(draft, chip));
});
