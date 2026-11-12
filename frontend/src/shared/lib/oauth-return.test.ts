/** Tests for the OAuth return path check. */
import assert from "node:assert/strict";
import { test } from "node:test";

import { callbackPath, safeReturnPath } from "./oauth-return.ts";

test("safeReturnPath keeps in-app paths only", () => {
  assert.equal(safeReturnPath("/submit"), "/submit");
  assert.equal(safeReturnPath("/submit?folder=1"), "/submit?folder=1");
  assert.equal(safeReturnPath("//evil.example"), null);
  assert.equal(safeReturnPath("/\\evil.example"), null);
  assert.equal(safeReturnPath("https://evil.example"), null);
  assert.equal(safeReturnPath(""), null);
  assert.equal(safeReturnPath(null), null);
});

test("callbackPath keeps same-origin paths and treats loopback hosts as one", () => {
  const mac = "http://127.0.0.1:3012";
  assert.equal(callbackPath("http://localhost:3012/mcp/authorize?state=y", mac), "/mcp/authorize?state=y");
  assert.equal(callbackPath("http://[::1]:3012/share/t#x", mac), "/share/t#x");
  assert.equal(callbackPath("/runs/1", mac), "/runs/1");
  assert.equal(callbackPath("http://localhost:4000/x", mac), null);
  assert.equal(callbackPath("https://localhost:3012/x", mac), null);
  assert.equal(callbackPath("http://evil.example:3012/x", mac), null);
  const web = "https://app.skynetml.com";
  assert.equal(callbackPath("https://app.skynetml.com/mcp/authorize?a=1", web), "/mcp/authorize?a=1");
  assert.equal(callbackPath("http://localhost/x", web), null);
  assert.equal(callbackPath("//evil.example/x", web), null);
  assert.equal(callbackPath("https://app.skynetml.com//evil.example", web), null);
  assert.equal(callbackPath("http://localhost:3012//evil.example/x", mac), null);
});
