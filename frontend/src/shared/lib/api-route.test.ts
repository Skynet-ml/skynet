import assert from "node:assert/strict";
import { test } from "node:test";

import {
  apiPath,
  hostedAvailable,
  hostedRouteOpen,
  isHostedPath,
  pairHref,
  parseSourceKey,
  resolveSource,
  runHref,
  sourceFromParam,
  sourceKey,
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

test("sourceKey keeps local IDs bare and keeps hosted IDs apart", () => {
  assert.equal(sourceKey("local", "abc"), "abc");
  assert.equal(sourceKey("hosted", "abc"), "hosted:abc");
  assert.notEqual(sourceKey("hosted", "abc"), sourceKey("local", "abc"));
  assert.deepEqual(parseSourceKey("hosted:abc"), { source: "hosted", id: "abc" });
  assert.deepEqual(parseSourceKey("abc"), { source: "local", id: "abc" });
});

test("hostedRouteOpen closes keys, payments, protected settings and account actions", () => {
  for (const closed of [
    "/billing/byok/keys",
    "/billing/byok/keys/abc",
    "/billing/payment-methods?x=1",
    "/billing/checkout",
    "/billing/portal",
    "/billing/subscription",
    "/agent/protected-settings",
    "/account/delete",
    "/account/export",
  ]) {
    assert.equal(hostedRouteOpen(closed), false, closed);
  }
  for (const open of [
    "/billing/usage?start=1",
    "/billing/transactions",
    "/agent/conversations",
    "/billing/byok",
  ]) {
    assert.equal(hostedRouteOpen(open), true, open);
  }
});
