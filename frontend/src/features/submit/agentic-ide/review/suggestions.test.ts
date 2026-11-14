/** Tests for the editor's inline completion logic (SKY-703). */

import assert from "node:assert/strict";
import test from "node:test";

import {
  cleanCompletion,
  completionWindow,
  consumeTyped,
  findLine,
  nextEditLine,
  nextWord,
  PREFIX_CHARS,
  shouldSuggest,
  SUFFIX_CHARS,
} from "./suggestions.ts";

const classify = "def classify(ticket: Ticket) -> str:\n    return x\n";
const next = { after: "def classify(ticket: Ticket) -> str:", lines: ["    if is_security(ticket):", '        return "security"'] };

test("the window cuts the text around the caret to the caps", () => {
  const doc = "a".repeat(PREFIX_CHARS + 10) + "|" + "b".repeat(SUFFIX_CHARS + 10);
  const pos = PREFIX_CHARS + 10;
  const { prefix, suffix } = completionWindow(doc, pos);
  assert.equal(prefix.length, PREFIX_CHARS);
  assert.equal(suffix.length, SUFFIX_CHARS);
  assert.ok(suffix.startsWith("|"));
  assert.deepEqual(completionWindow("ab", 1), { prefix: "a", suffix: "b" });
});

test("a suggestion is asked at the end of a line or before closing brackets only", () => {
  assert.ok(shouldSuggest("def is_s", ""));
  assert.ok(shouldSuggest("print(x", ")"));
  assert.ok(shouldSuggest("x = f(a", "):  "));
  assert.equal(shouldSuggest("   ", ""), false);
  assert.equal(shouldSuggest("pri", "nt(x)"), false);
});

test("the answer loses trailing space and an echo of the rest of the line", () => {
  assert.equal(cleanCompletion("a, b)\n\n", ")"), "a, b");
  assert.equal(cleanCompletion("urn a + b  ", ""), "urn a + b");
  assert.equal(cleanCompletion(")", ")"), "");
  assert.equal(cleanCompletion("x)", "y"), "x)");
});

test("typing through a ghost eats into it, and a mismatch drops it", () => {
  assert.equal(consumeTyped("ecurity(ticket)", "ec"), "urity(ticket)");
  assert.equal(consumeTyped("ecurity", "x"), null);
  assert.equal(consumeTyped("ecurity", "ecurity"), null);
  assert.equal(consumeTyped("ecurity", ""), null);
});

test("next word keeps leading space with the word", () => {
  assert.equal(nextWord("ecurity(ticket"), "ecurity");
  assert.equal(nextWord("(ticket"), "(");
  assert.equal(nextWord("\n    text = 1"), "\n    text");
  assert.equal(nextWord("   "), "   ");
});

test("the next edit anchors on its line, and not twice", () => {
  assert.equal(findLine(classify, "def classify(ticket: Ticket) -> str:"), 1);
  assert.equal(findLine(classify, "nothing"), 0);
  assert.equal(nextEditLine(classify, next), 1);
  assert.equal(nextEditLine(classify + "    if is_security(ticket):\n", next), 0);
  assert.equal(nextEditLine("", next), 0);
  assert.equal(nextEditLine(classify, null), 0);
  assert.equal(nextEditLine(classify, { after: next.after, lines: [] }), 0);
  assert.equal(nextEditLine(classify, { after: "  ", lines: ["x"] }), 0);
});
