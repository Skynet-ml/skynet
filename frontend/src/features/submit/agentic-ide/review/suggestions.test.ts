/** Tests for the editor's inline completion logic (SKY-703). */

import assert from "node:assert/strict";
import test from "node:test";

import {
  balanced,
  cleanCompletion,
  completionWindow,
  consumeTyped,
  findLine,
  nextEditLine,
  nextWord,
  pausesCompletions,
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
  assert.equal(cleanCompletion("a, b)\n\n", "f(", ")"), "a, b");
  assert.equal(cleanCompletion("urn a + b  ", "    ret", ""), "urn a + b");
  assert.equal(cleanCompletion(")", "print(x", ")"), "");
  assert.equal(cleanCompletion("x)", "f(", "y"), "x)");
  assert.equal(cleanCompletion('hello"', 'x = "', '"'), "hello");
  assert.equal(cleanCompletion("f(x)]", "a = [", "]"), "f(x)");
});

test("a closing bracket or quote that the line needs stays whole", () => {
  assert.equal(cleanCompletion("bar(1)", "foo(", ")"), "bar(1)");
  assert.equal(cleanCompletion("x[0]", "a = [", "]"), "x[0]");
  assert.equal(cleanCompletion('"a"', 'x = "', '"'), '"a"');
  assert.equal(cleanCompletion('"a"', "x = ", '"'), '"a"', "the answer opens and closes its own string");
  assert.equal(cleanCompletion("x)", "foo(bar(", ")"), "x)");
});

test("an apostrophe inside a string is plain text", () => {
  assert.equal(cleanCompletion(`'t know")`, 'print("don', '")'), "'t know");
  assert.equal(cleanCompletion(`'t do this."""`, '    """Don', '"""'), "'t do this.");
});

test("a closer for a bracket the answer opened stays, also with an opener on an earlier line", () => {
  assert.equal(cleanCompletion("int(x)", "    value=", ")"), "int(x)");
  assert.equal(cleanCompletion("f(b)", "    a, ", "):"), "f(b)");
  assert.equal(cleanCompletion('get("k")', "    name=", ")"), 'get("k")');
  assert.equal(cleanCompletion("len(x)", "print(", ")"), "len(x)");
  assert.equal(cleanCompletion("x)", "foo(", ")"), "x");
});

test("a bracket opened on an earlier line keeps the answer whole", () => {
  assert.equal(cleanCompletion("y)", "    x, ", ")"), "y)");
  assert.equal(cleanCompletion("b)\n", "    a, ", "):"), "b)");
});

test("balance counts brackets outside strings, escapes and triple quotes", () => {
  assert.ok(balanced("f(x[0], {a: 1})"));
  assert.ok(balanced('print(")")'));
  assert.ok(balanced('"a\\"b"'));
  assert.ok(balanced(`print("don't")`));
  assert.ok(balanced('"""a " b"""'));
  assert.equal(balanced("a)"), false);
  assert.equal(balanced("(a]"), false);
  assert.equal(balanced('"open'), false);
  assert.equal(balanced('"""a""""""'), false);
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

test("a refusal or a provider failure pauses requests, other answers do not", () => {
  for (const status of [402, 403, 409, 422, 424, 429, 502, 503]) assert.ok(pausesCompletions(status), String(status));
  for (const status of [200, 400, 401, 404, 500]) assert.equal(pausesCompletions(status), false, String(status));
});
