import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { AUTH_GATED_PATTERN } from "./shared/lib/proxy-route.ts";

// API routes that must stay behind login. Every other ``src/app/api`` route is
// hit by logged-out visitors and must be excluded from the proxy matcher, or
// its POSTs bounce to /login and the flow silently never completes.
const GATED_API = new Set<string>(["account-link"]);

test("every API route is either excluded from the auth matcher or explicitly gated", () => {
  const excluded = new Set(
    [...AUTH_GATED_PATTERN.matchAll(/api\/([\w-]+)(?=[|)])/g)].map((m) => m[1]),
  );
  const routes = fs
    .readdirSync(path.join(process.cwd(), "src", "app", "api"), { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name);
  const unaccounted = routes.filter((name) => !excluded.has(name) && !GATED_API.has(name));
  assert.deepEqual(unaccounted, []);
});

// Signed-out pages (login, terms, privacy, public runs) load ``public/`` assets
// such as the bundled fonts. A gated asset is answered with the /login HTML.
test("every public/ asset is excluded from the auth matcher", () => {
  const matcher = new RegExp(`^${AUTH_GATED_PATTERN}$`);
  const publicDir = path.join(process.cwd(), "public");
  const assets = fs
    .readdirSync(publicDir, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => "/" + path.relative(publicDir, path.join(entry.parentPath, entry.name)));
  assert.ok(assets.includes("/fonts/onest-latin-wght-normal.woff2"));
  assert.deepEqual(
    assets.filter((asset) => matcher.test(asset)),
    [],
  );
  assert.ok(matcher.test("/datasets"), "app routes must stay gated");
});

// Local mode refuses a foreign Host in the proxy, so the proxy must run on the
// NextAuth routes and the signed-out pages too, not only on gated routes.
test("the proxy runs on every route except build output", () => {
  const source = fs.readFileSync(path.join(process.cwd(), "src", "proxy.ts"), "utf8");
  const pattern = source.match(/matcher: \["(.*)"\]/)?.[1];
  assert.ok(pattern, "matcher not found in proxy.ts");
  const matcher = new RegExp(`^${pattern}$`);
  for (const route of [
    "/api/auth/session",
    "/api/auth/callback/local",
    "/login",
    "/fonts/a.woff2",
    "/datasets",
  ]) {
    assert.ok(matcher.test(route), route);
  }
  assert.ok(!matcher.test("/_next/static/chunks/main.js"));
});

// The docs and the blog are public: readers, crawlers and language models read them signed-out.
test("the public docs, blog and llms.txt skip the login gate", () => {
  const matcher = new RegExp(`^${AUTH_GATED_PATTERN}$`);
  for (const route of [
    "/docs",
    "/docs/quickstart",
    "/docs/he/concepts/runs",
    "/docs.md",
    "/docs/mcp.md",
    "/docs-md/mcp",
    "/docs-search",
    "/llms.txt",
    "/llms-full.txt",
    "/blog",
    "/blog/the-most-valuable-skill-of-this-century",
    "/blog/the-most-valuable-skill-of-this-century/timeline.svg",
  ]) {
    assert.ok(!matcher.test(route), route);
  }
  assert.ok(matcher.test("/llms.json"), "only the two llms text files are public");
});

// The bare root is the signed-out landing page (SKY-922); every app route stays gated.
test("the root is public and the app routes stay gated", () => {
  const matcher = new RegExp(`^${AUTH_GATED_PATTERN}$`);
  assert.ok(!matcher.test("/"));
  for (const route of ["/datasets", "/submit", "/x", "/settings"]) {
    assert.ok(matcher.test(route), route);
  }
});
