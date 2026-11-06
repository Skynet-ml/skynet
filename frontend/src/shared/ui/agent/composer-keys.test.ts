import assert from "node:assert/strict";
import test from "node:test";

import { handleComposerEscape } from "./composer-keys.ts";

test("Escape stops a streaming composer through its Stop callback", () => {
  let stops = 0;
  let prevented = false;
  const handled = handleComposerEscape({ key: "Escape", preventDefault: () => { prevented = true; } }, {
    streaming: true, popupOpen: false, composing: false,
  }, () => { stops += 1; });
  assert.equal(handled, true);
  assert.equal(prevented, true);
  assert.equal(stops, 1);
});

for (const state of [
  { streaming: false, popupOpen: false, composing: false },
  { streaming: true, popupOpen: true, composing: false },
  { streaming: true, popupOpen: false, composing: true },
]) {
  test(`Escape does not stop with ${JSON.stringify(state)}`, () => {
    assert.equal(handleComposerEscape({ key: "Escape", preventDefault: () => assert.fail("consumed") }, state,
      () => assert.fail("stopped")), false);
  });
}

test("a consumed Escape or another key does not stop", () => {
  for (const event of [{ key: "Escape", defaultPrevented: true }, { key: "Enter" }]) {
    assert.equal(handleComposerEscape({ ...event, preventDefault: () => assert.fail("consumed") }, {
      streaming: true, popupOpen: false, composing: false,
    }, () => assert.fail("stopped")), false);
  }
});
