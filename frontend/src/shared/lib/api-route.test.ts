import assert from "node:assert/strict";
import { test } from "node:test";

import {
  apiPath,
  hostedAvailable,
  isHostedPath,
  pairHref,
  resolveSource,
  runHref,
  sourceFromParam,
} from "./api-route.ts";

test("apiPath keeps local paths and prefixes hosted ones with the proxy", () => {
  assert.equal(apiPath("/optimizations/sidebar?limit=20"), "/optimizations/sidebar?limit=20");
  assert.equal(apiPath("/optimizations/abc", "local"), "/optimizations/abc");
  assert.equal(apiPath("/optimizations/abc", "hosted"), "/local/hosted/optimizations/abc");
});

test("isHostedPath matches only the proxy, not the link routes", () => {
  assert.equal(isHostedPath("/local/hosted/optimizations"), true);
  assert.equal(isHostedPath("http://127.0.0.1:8000/local/hosted/optimizations/x/stream"), true);
  assert.equal(isHostedPath("/local/hosted-link/status"), false);
  assert.equal(isHostedPath("/optimizations"), false);
});

test("hosted is used only in local mode with a linked account", () => {
  assert.equal(hostedAvailable(true, true), true);
  assert.equal(hostedAvailable(true, false), false);
  assert.equal(hostedAvailable(false, true), false);
  assert.equal(resolveSource("hosted", true, true), "hosted");
  assert.equal(resolveSource("hosted", true, false), "local");
  assert.equal(resolveSource("hosted", false, true), "local");
  assert.equal(resolveSource("hosted", false, false), "local");
  assert.equal(resolveSource("local", true, true), "local");
  assert.equal(resolveSource(undefined, true, true), "local");
});

test("run pages carry the source only for hosted runs", () => {
  assert.equal(runHref("abc"), "/optimizations/abc");
  assert.equal(runHref("abc", "local"), "/optimizations/abc");
  assert.equal(runHref("abc", "hosted"), "/optimizations/abc?source=hosted");
  assert.equal(sourceFromParam("hosted"), "hosted");
  assert.equal(sourceFromParam("local"), "local");
  assert.equal(sourceFromParam("other"), "local");
  assert.equal(sourceFromParam(null), "local");
});

test("pairHref keeps the source and adds the pair param", () => {
  assert.equal(pairHref("abc", 2), "/optimizations/abc?pair=2");
  assert.equal(pairHref("abc", 0, "hosted"), "/optimizations/abc?source=hosted&pair=0");
  assert.equal(pairHref("abc", null, "hosted"), "/optimizations/abc?source=hosted");
  assert.equal(pairHref("abc", null), "/optimizations/abc");
});
