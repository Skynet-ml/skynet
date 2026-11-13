/** Tests for the dictation wave's level helper. */

import assert from "node:assert/strict";
import test from "node:test";

import { frameLevel } from "./dictation-level.ts";

const frame = (amp: number, n = 512) =>
  Uint8Array.from({ length: n }, (_, i) => Math.round(128 + amp * 127 * Math.sin(i / 4)));

test("silence and room noise stay flat", () => {
  assert.equal(frameLevel(new Uint8Array(0)), 0);
  assert.equal(frameLevel(frame(0)), 0);
  assert.ok(frameLevel(frame(0.01)) < 0.05);
});

test("louder speech gives a higher level, capped at 1", () => {
  const quiet = frameLevel(frame(0.1));
  const loud = frameLevel(frame(0.4));
  assert.ok(quiet > 0.2, `quiet speech moves the line (${quiet})`);
  assert.ok(loud > quiet);
  assert.equal(frameLevel(frame(1)), 1);
});
