/** Contract: every navigable route is in the page registry that feeds Cmd+K search. */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { APP_PAGES, matchesQuery } from "./app-pages.ts";

const appRoot = path.join(process.cwd(), "src", "app");
const enCatalog = JSON.parse(
  fs.readFileSync(path.join(process.cwd(), "..", "i18n", "locales", "ui", "en.json"), "utf8"),
) as Record<string, string>;

// Routes a signed-in user never navigates to by name.
const NOT_SEARCHABLE = new Set(["/login", "/desktop/link", "/mcp/authorize"]);

function staticRoutes(dir: string, segments: string[] = []): string[] {
  const routes: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.isFile() && entry.name === "page.tsx") {
      routes.push(`/${segments.join("/")}`);
    } else if (entry.isDirectory() && !entry.name.startsWith("[") && !entry.name.startsWith("_")) {
      // Route groups like "(dashboard)" don't add a URL segment.
      const next = entry.name.startsWith("(") ? segments : [...segments, entry.name];
      routes.push(...staticRoutes(path.join(dir, entry.name), next));
    }
  }
  return routes;
}

test("every static route is registered for search", () => {
  const registered = new Set(APP_PAGES.map((page) => page.href.split("?")[0]));
  const missing = staticRoutes(appRoot).filter(
    (route) => !NOT_SEARCHABLE.has(route) && !registered.has(route),
  );
  assert.deepEqual(missing, [], `add these routes to APP_PAGES in app-pages.ts: ${missing}`);
});

test("every registry message key exists in the English catalog", () => {
  for (const page of APP_PAGES) {
    const keys = [
      page.keywordsKey,
      page.descriptionKey,
      "key" in page.label ? page.label.key : undefined,
    ];
    for (const key of keys) {
      if (key) assert.ok(enCatalog[key], `${page.id}: missing message key ${key}`);
    }
  }
});

test("page ids are unique", () => {
  const ids = APP_PAGES.map((page) => page.id);
  assert.equal(new Set(ids).size, ids.length);
});

test("downloads follows Storage before the legal pages in global search", () => {
  const ids = APP_PAGES.filter((page) => page.group === "navigate").map((page) => page.id);
  const storage = ids.indexOf("storage");
  assert.ok(storage >= 0);
  assert.deepEqual(ids.slice(storage, storage + 4), ["storage", "download", "terms", "privacy"]);
});

test("matchesQuery needs every term, in any order", () => {
  const haystack = ["API", "API token developer"];
  assert.ok(matchesQuery(haystack, "token api"));
  assert.ok(matchesQuery(haystack, "  "));
  assert.ok(!matchesQuery(haystack, "api billing"));
});
