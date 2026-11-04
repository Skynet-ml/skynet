/** Tests for the OAuth return path check. */
import assert from "node:assert/strict";
import { test } from "node:test";

import { safeReturnPath } from "./oauth-return.ts";

test("safeReturnPath keeps in-app paths only", () => {
  assert.equal(safeReturnPath("/submit"), "/submit");
  assert.equal(safeReturnPath("/submit?folder=1"), "/submit?folder=1");
  assert.equal(safeReturnPath("//evil.example"), null);
  assert.equal(safeReturnPath("/\\evil.example"), null);
  assert.equal(safeReturnPath("https://evil.example"), null);
  assert.equal(safeReturnPath(""), null);
  assert.equal(safeReturnPath(null), null);
});
