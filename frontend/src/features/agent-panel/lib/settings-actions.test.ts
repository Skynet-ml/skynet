import assert from "node:assert/strict";
import { test } from "node:test";

import { parseAgentNavigation, parseAppearancePatch } from "./settings-actions.ts";

test("open_page results become a settings tab or an in-app route", () => {
  assert.deepEqual(parseAgentNavigation({ navigate: { kind: "settings", tab: "billing" } }), {
    kind: "settings",
    tab: "billing",
  });
  assert.deepEqual(parseAgentNavigation('{"navigate":{"kind":"route","path":"/tagger"}}'), {
    kind: "route",
    path: "/tagger",
  });
});

test("unsafe or malformed navigation is ignored", () => {
  for (const path of ["//evil.example", "https://evil.example", "/../x", "javascript:alert(1)"]) {
    assert.equal(parseAgentNavigation({ navigate: { kind: "route", path } }), null);
  }
  assert.equal(parseAgentNavigation({ navigate: { kind: "settings", tab: "" } }), null);
  assert.equal(parseAgentNavigation("not json"), null);
  assert.equal(parseAgentNavigation(null), null);
});

// SKY-631: the chat opens the tagger on a dataset with a narrow query string.
test("a route keeps a narrow query string and rejects open-redirect shapes", () => {
  assert.deepEqual(parseAgentNavigation({ navigate: { kind: "route", path: "/tagger?dataset=abc-123" } }), {
    kind: "route",
    path: "/tagger?dataset=abc-123",
  });
  for (const path of [
    "//evil.com",
    "/tagger?next=https://x",
    "/tagger?next=//evil.com",
    "/tagger?a=b\\c",
    "/tagger?a=user@evil.com",
    "/tagger?a=b#frag",
    "/tagger#frag",
    "/tagger?",
    "/tagger?a=1?b=2",
    "/tagger?a=%2F%2Fevil.com",
  ]) {
    assert.equal(parseAgentNavigation({ navigate: { kind: "route", path } }), null, path);
  }
});

test("a route path is at most 200 characters in total, as on the backend", () => {
  const at200 = `/tagger?dataset=${"a".repeat(200 - "/tagger?dataset=".length)}`;
  assert.equal(at200.length, 200);
  assert.deepEqual(parseAgentNavigation({ navigate: { kind: "route", path: at200 } }), {
    kind: "route",
    path: at200,
  });
  assert.equal(parseAgentNavigation({ navigate: { kind: "route", path: `${at200}a` } }), null);
});

test("appearance patches keep only string theme and locale", () => {
  assert.deepEqual(parseAppearancePatch({ ok: true, client_patch: { theme: "dark", locale: "he" } }), {
    theme: "dark",
    locale: "he",
  });
  assert.deepEqual(parseAppearancePatch({ client_patch: { theme: 3 } }), {});
  assert.deepEqual(parseAppearancePatch({ ok: true, result: { hold_minutes: 5 } }), {});
});
