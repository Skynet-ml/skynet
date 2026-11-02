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
  for (const path of ["//evil.example", "https://evil.example", "/a?b=1", "/../x", "javascript:alert(1)"]) {
    assert.equal(parseAgentNavigation({ navigate: { kind: "route", path } }), null);
  }
  assert.equal(parseAgentNavigation({ navigate: { kind: "settings", tab: "" } }), null);
  assert.equal(parseAgentNavigation("not json"), null);
  assert.equal(parseAgentNavigation(null), null);
});

test("appearance patches keep only string theme and locale", () => {
  assert.deepEqual(parseAppearancePatch({ ok: true, client_patch: { theme: "dark", locale: "he" } }), {
    theme: "dark",
    locale: "he",
  });
  assert.deepEqual(parseAppearancePatch({ client_patch: { theme: 3 } }), {});
  assert.deepEqual(parseAppearancePatch({ ok: true, result: { hold_minutes: 5 } }), {});
});
