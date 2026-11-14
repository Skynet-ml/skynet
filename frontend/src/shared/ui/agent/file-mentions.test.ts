/** Tests for the "@path" file mention menu's matching and insertion. */

import assert from "node:assert/strict";
import test from "node:test";

import { insertMention, matchMentions, mentionAt } from "./file-mentions.ts";

const PATHS = ["README.md", "src/triage/classifier.py", "tests/test_classifier.py", "metric.py"];

test("mentionAt finds a mention at the start or after a space", () => {
  assert.deepEqual(mentionAt("@cla", 4), { start: 0, query: "cla" });
  assert.deepEqual(mentionAt("look at @src/tr", 15), { start: 8, query: "src/tr" });
  assert.deepEqual(mentionAt("@", 1), { start: 0, query: "" });
});

test("mentionAt ignores an email, a finished mention and a caret inside a word", () => {
  assert.equal(mentionAt("a@b", 3), null);
  assert.equal(mentionAt("@run.py ", 8), null);
  assert.equal(mentionAt("@runpy", 3), null);
});

test("matchMentions ranks file-name prefix matches first", () => {
  assert.deepEqual(matchMentions(PATHS, "test"), ["tests/test_classifier.py"]);
  assert.deepEqual(matchMentions(PATHS, "class"), [
    "src/triage/classifier.py",
    "tests/test_classifier.py",
  ]);
  assert.deepEqual(matchMentions(PATHS, "triage"), ["src/triage/classifier.py"]);
});

test("matchMentions lists everything for an empty query, up to the limit", () => {
  assert.deepEqual(matchMentions(PATHS, "", 2), ["README.md", "src/triage/classifier.py"]);
});

test("insertMention replaces the query and puts the caret after one space", () => {
  assert.deepEqual(insertMention("see @cla now", 8, 4, "src/triage/classifier.py"), {
    text: "see @src/triage/classifier.py now",
    caret: 30,
  });
});
