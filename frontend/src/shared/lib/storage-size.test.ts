import assert from "node:assert/strict";
import { test } from "node:test";

import { formatStorageSize } from "./storage-size.ts";

test("formatStorageSize shows the Free and Pro budgets as round values", () => {
  assert.equal(formatStorageSize(500 * 1024 ** 2), "500 MB");
  assert.equal(formatStorageSize(20 * 1024 ** 3), "20 GB");
});

test("formatStorageSize escalates to GB past 1024 MB and keeps small sizes", () => {
  assert.equal(formatStorageSize(1.5 * 1024 ** 3), "1.5 GB");
  assert.equal(formatStorageSize(1023 * 1024 ** 2), "1023 MB");
  assert.equal(formatStorageSize(524_288), "512 KB");
  assert.equal(formatStorageSize(0), "0 B");
  assert.equal(formatStorageSize(null), "0 B");
});

test("formatStorageSize moves up a unit when rounding reaches 1024", () => {
  assert.equal(formatStorageSize(1024 ** 3 - 1), "1 GB");
  assert.equal(formatStorageSize(1024 ** 2 - 1), "1 MB");
});
