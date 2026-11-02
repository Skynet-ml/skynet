import { test } from "node:test";
import assert from "node:assert/strict";

import {
  endSplash,
  playsEntrance,
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
  for (const path of [
    "/login",
    "/terms",
    "/privacy",
    "/share/abc",
    "/docs",
    "/docs/he/start",
    "/desktop/link",
    "/mcp/authorize",
  ]) {
    assert.equal(setupCoversPath(path), false, path);
  }
  for (const path of ["/", "/jobs", "/settings", "/docsearch"]) {
    assert.equal(setupCoversPath(path), true, path);
  }
});

const fresh = { languageAnswered: false, rerunning: false, splash: "playing" } as const;

test("a fresh setup under the playing splash plays the entrance", () => {
  assert.equal(playsEntrance(fresh), true);
});

test("a resumed setup with the language answered skips the entrance, so the splash fades", () => {
  assert.equal(playsEntrance({ ...fresh, languageAnswered: true }), false);
  assert.equal(playsEntrance({ ...fresh, rerunning: true }), false);
});

test("a splash that already left never gives a later setup the entrance", () => {
  assert.equal(playsEntrance({ ...fresh, splash: "handoff" }), false);
  assert.equal(playsEntrance({ ...fresh, splash: "gone" }), false);
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

test("a fade marks the splash gone at once, so a setup that opens during the fade plays its glance", async () => {
  // The splash state is per page load, so this case needs its own module instance.
  const fade: typeof import("./entrance.ts") = await import("./entrance.ts?fade");
  fade.commitSplashExit("fade");
  assert.equal(fade.readSplashState(), "gone");
  assert.equal(playsEntrance({ ...fresh, splash: fade.readSplashState() }), false);

  const handoff: typeof import("./entrance.ts") = await import("./entrance.ts?handoff");
  handoff.commitSplashExit("handoff");
  assert.equal(handoff.readSplashState(), "handoff");
});
