import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveResourceMode } from "./resource-mode.ts";

test("the web app always shows the web account", () => {
  assert.equal(resolveResourceMode(null, false, false), "hosted");
  assert.equal(resolveResourceMode("local", false, true), "hosted");
});

test("an unlinked Mac shows only this Mac, whatever was stored", () => {
  assert.equal(resolveResourceMode("hosted", true, false), "local");
  assert.equal(resolveResourceMode(null, true, false), "local");
});

test("a linked Mac starts on Web and keeps a Local pick", () => {
  assert.equal(resolveResourceMode(null, true, true), "hosted");
  assert.equal(resolveResourceMode("junk", true, true), "hosted");
  assert.equal(resolveResourceMode("local", true, true), "local");
});
