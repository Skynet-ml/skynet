import { test } from "node:test";
import assert from "node:assert/strict";

import {
  endSplash,
  readSplashState,
  setupCoversPath,
  splashExit,
  subscribeSplash,
} from "./entrance.ts";

const base = {
  session: "authenticated",
  loaded: true,
  setupShows: false,
  holdExpired: false,
} as const;

test("a waiting setup takes the splash over with no fade", () => {
  assert.equal(splashExit({ ...base, setupShows: true }), "handoff");
  assert.equal(splashExit({ ...base, setupShows: true, holdExpired: true }), "handoff");
});

test("a signed-in user with the setup done sees the normal fade", () => {
  assert.equal(splashExit(base), "fade");
});

test("the splash holds for an unknown setup state, but not past the limit", () => {
  assert.equal(splashExit({ ...base, loaded: false }), "hold");
  assert.equal(splashExit({ ...base, session: "loading", loaded: false }), "hold");
  assert.equal(splashExit({ ...base, loaded: false, holdExpired: true }), "fade");
});

test("a signed-out visitor never waits for a setup", () => {
  assert.equal(splashExit({ ...base, session: "unauthenticated", loaded: false }), "fade");
});

test("the setup never covers the sign-in or public pages", () => {
  for (const path of ["/login", "/terms", "/privacy", "/share/abc"]) {
    assert.equal(setupCoversPath(path), false, path);
  }
  for (const path of ["/", "/jobs", "/settings"]) assert.equal(setupCoversPath(path), true, path);
});

test("the splash ends once per page load and tells its listeners", () => {
  assert.equal(readSplashState(), "playing");
  let calls = 0;
  const off = subscribeSplash(() => calls++);
  endSplash("handoff");
  endSplash("gone");
  off();
  assert.equal(readSplashState(), "handoff");
  assert.equal(calls, 1);
});
