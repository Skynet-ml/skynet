/** Tests for the port of IntelliJ's TypoTolerantMatcher, with the "*" + prefix that completion uses. */

import assert from "node:assert/strict";
import test from "node:test";

import { TypoTolerantMatcher } from "./pycharm-typo.ts";

const matcher = (prefix: string) => new TypoTolerantMatcher(`*${prefix}`, "first-letter");

test("swapped letters match with one error", () => {
  assert.deepEqual(matcher("gte").match("get"), [[0, 3, 1]]);
});

test("a neighbouring key on the keyboard matches with one error", () => {
  // "r" sits right of "e", so leftMiss("r") is "e".
  assert.deepEqual(matcher("grt").match("get"), [[0, 3, 1]]);
});

test("a missing letter matches with one error", () => {
  assert.deepEqual(matcher("gt").match("get"), [[0, 3, 1]]);
});

test("letters that are no typo of the name do not match", () => {
  assert.equal(matcher("xyz").matches("get"), false);
  assert.equal(matcher("xyz").match("get"), null);
});

test("an exact pattern keeps the camel-hump fragments and no errors", () => {
  const m = matcher("CU");
  assert.deepEqual(m.match("CurrentUser"), [
    [0, 1, 0],
    [7, 8, 0],
  ]);
  assert.deepEqual(m.matchingFragments("CurrentUser"), [
    [0, 1],
    [7, 8],
  ]);
});

test("each typo lowers the matching degree by 2000 * (errors / length)^2", () => {
  // 1000 word start + 1 hump case - 1 fragment - 222 errors + 2 no separator + 1 start + 1 final.
  assert.equal(matcher("gte").matchingDegree("get"), 782);
  assert.equal(matcher("gte").matchingDegree("get", false, null), -2147483648);
});
