import { test } from "node:test";
import assert from "node:assert/strict";

import { requestedDetailTab, shownDetailTab } from "./detail-tabs.ts";

test("no tab param opens Overview, and renamed tabs land on Usage", () => {
  assert.equal(requestedDetailTab(null), "overview");
  assert.equal(requestedDetailTab("lm-activity"), "usage");
  assert.equal(requestedDetailTab("budget"), "usage");
  assert.equal(requestedDetailTab("logs"), "logs");
});

test("an unknown or removed tab opens Overview", () => {
  assert.equal(requestedDetailTab("playground"), "overview");
  assert.equal(requestedDetailTab("no-such-tab"), "overview");
});

test("phones open desk-only tabs on Overview but keep view-first ones", () => {
  assert.equal(shownDetailTab("data", true), "overview");
  assert.equal(shownDetailTab("config", true), "overview");
  assert.equal(shownDetailTab("logs", true), "logs");
  assert.equal(shownDetailTab("data", false), "data");
});
