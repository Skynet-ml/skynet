import { test } from "node:test";
import assert from "node:assert/strict";
import { isRefusedLocalHost, proxyRoute } from "./proxy-route.ts";

const desktop = { APP_LOCAL_MODE: "1", AUTH_URL: "http://127.0.0.1:48120" };

test("hosted builds refuse no host", () => {
  assert.equal(isRefusedLocalHost("evil.example", {}), false);
  assert.equal(isRefusedLocalHost(null, { AUTH_URL: "https://staging.skynetml.com" }), false);
});

test("local mode serves the AUTH_URL host and the loopback names on its port", () => {
  for (const host of ["127.0.0.1:48120", "localhost:48120", "LOCALHOST:48120"]) {
    assert.equal(isRefusedLocalHost(host, desktop), false, host);
  }
});

test("local mode refuses a rebound or foreign host", () => {
  for (const host of [
    "evil.example:48120",
    "evil.example",
    "127.0.0.1.evil.example:48120",
    "127.0.0.1:48121",
    "127.0.0.1",
    "user@127.0.0.1:48120",
    "127.0.0.1:48120/x",
    "[::1]:48120",
    "",
    null,
  ]) {
    assert.equal(isRefusedLocalHost(host, desktop), true, String(host));
  }
});

test("local dev without AUTH_URL serves the loopback names on any port", () => {
  const dev = { APP_LOCAL_MODE: "1" };
  assert.equal(isRefusedLocalHost("localhost:3000", dev), false);
  assert.equal(isRefusedLocalHost("127.0.0.1:3002", dev), false);
  assert.equal(isRefusedLocalHost("evil.example:3000", dev), true);
});

test("local mode refuses a foreign host before the login gate decides", () => {
  for (const path of ["/api/auth/session", "/api/auth/callback/local", "/login", "/datasets"]) {
    assert.equal(proxyRoute("evil.example:48120", path, desktop), "refuse", path);
  }
});

test("local mode keeps the normal routing for its own host", () => {
  assert.equal(proxyRoute("127.0.0.1:48120", "/api/auth/session", desktop), "pass");
  assert.equal(proxyRoute("localhost:48120", "/login", desktop), "pass");
  assert.equal(proxyRoute("127.0.0.1:48120", "/datasets", desktop), "auth");
});

test("hosted builds route by path only", () => {
  assert.equal(proxyRoute("staging.skynetml.com", "/api/auth/session", {}), "pass");
  assert.equal(proxyRoute("staging.skynetml.com", "/datasets", {}), "auth");
});

test("signed-out pages load the brand icons and artwork", () => {
  for (const path of ["/favicon.png", "/favicon-dark.png", "/apple-icon.png", "/brand/hoatzin-light-256.webp"]) {
    assert.equal(proxyRoute("staging.skynetml.com", path, {}), "pass", path);
  }
});

test("a shared agent chat opens signed-out; an optimization share link stays gated", () => {
  assert.equal(proxyRoute("staging.skynetml.com", "/share/chat/abc123", {}), "pass");
  assert.equal(proxyRoute("staging.skynetml.com", "/share/abc123", {}), "auth");
  assert.equal(proxyRoute("staging.skynetml.com", "/share/chat", {}), "auth");
});
