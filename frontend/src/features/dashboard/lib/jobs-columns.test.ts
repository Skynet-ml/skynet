import { test } from "node:test";
import assert from "node:assert/strict";
import { jobsColSpan, visibleJobsColumns, DEFAULT_COL_WIDTHS } from "./jobs-columns.ts";

test("shared columns only show for lists that hold shared runs", () => {
  assert.equal(visibleJobsColumns(false).length, 6);
  assert.equal(visibleJobsColumns(true).length, 8);
  assert.ok(!visibleJobsColumns(false).some((c) => c.key === "username" || c.key === "role"));
});

test("the row span adds the checkbox and the chevron columns", () => {
  assert.equal(jobsColSpan(false), 8);
  assert.equal(jobsColSpan(true), 10);
});

test("every column has a default width", () => {
  assert.equal(DEFAULT_COL_WIDTHS.name, 104);
  assert.equal(Object.keys(DEFAULT_COL_WIDTHS).length, 8);
});
