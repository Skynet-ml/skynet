/** Tests for the editor's inline completion logic (SKY-703). */

import assert from "node:assert/strict";
import test from "node:test";

import {
  cleanCompletion,
  completionWindow,
  consumeTyped,
  findLine,
  hasExcess,
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
  assert.equal(cleanCompletion("x)", "foo(bar(", ")"), "x)");
  assert.equal(cleanCompletion("len(x)", "print(", ")"), "len(x)");
});

test("an echoed quote is cut, so the string closes once", () => {
  assert.equal(cleanCompletion('"a"', "x = ", '"'), '"a');
});

test("the cut keeps the space and line breaks before the echo", () => {
  assert.equal(cleanCompletion(': "', 'prompt = "Enter your name', '"'), ": ");
  assert.equal(cleanCompletion("\n    a,\n    b,\n)", "foo(", ")"), "\n    a,\n    b,\n");
});

test("an apostrophe inside a string is plain text", () => {
  assert.equal(cleanCompletion(`'t know")`, 'print("don', '")'), "'t know");
  assert.equal(cleanCompletion(`'t do this."""`, '    """Don', '"""'), "'t do this.");
});

test("a bracket opened on an earlier line in the window takes the closer", () => {
  assert.equal(cleanCompletion(", 2)", "values = dict(\n    a=f(1", ")"), ", 2)");
  assert.equal(cleanCompletion("int(x)", "compute(\n    value=", ")"), "int(x)");
  assert.equal(cleanCompletion("f(b)", "compute(\n    a, ", "):"), "f(b)");
  assert.equal(cleanCompletion('get("k")', "compute(\n    name=", ")"), 'get("k")');
  assert.equal(cleanCompletion("y)", "foo(\n    x, ", ")"), "y");
  assert.equal(cleanCompletion("x)", "foo(", ")"), "x");
});

test("without the opener in the window, both texts have an excess and the answer stays whole", () => {
  assert.equal(cleanCompletion("y)", "    x, ", ")"), "y)");
  assert.equal(cleanCompletion("b)\n", "    a, ", "):"), "b)");
});

test("the excess scan reads brackets outside strings, escapes and triple quotes", () => {
  assert.equal(hasExcess("f(x[0], {a: 1})"), false);
  assert.equal(hasExcess('print(")")'), false);
  assert.equal(hasExcess('"a\\"b"'), false);
  assert.equal(hasExcess(`print("don't")`), false);
  assert.equal(hasExcess('"""a " b"""'), false);
  assert.equal(hasExcess("f(\n  a,\n"), false, "an open bracket is not an excess");
  assert.equal(hasExcess("x = 'it\ny = (1)"), false, "a one-quote string ends at a line break");
  assert.ok(hasExcess("a)"));
  assert.ok(hasExcess("(a]"));
  assert.ok(hasExcess('"open'));
  assert.ok(hasExcess('"""a""""""'));
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
  const caret = classify.length;
  assert.equal(findLine(classify, "def classify(ticket: Ticket) -> str:", caret), 1);
  assert.equal(findLine(classify, "nothing", caret), 0);
  assert.equal(nextEditLine(classify, next, caret), 1);
  const applied = classify.replace("\n", "\n    if is_security(ticket):\n");
  assert.equal(nextEditLine(applied, next, caret), 0);
  assert.equal(nextEditLine("", next, 0), 0);
  assert.equal(nextEditLine(classify, null, caret), 0);
  assert.equal(nextEditLine(classify, { after: next.after, lines: [] }, caret), 0);
  assert.equal(nextEditLine(classify, { after: "  ", lines: ["x"] }, caret), 0);
});

test("the next edit takes the anchor nearest the caret, inside the window", () => {
  const block = "def f():\n    return result\n";
  const filler = "# pad\n".repeat(200);
  const doc = block + filler + block + filler + block;
  const lines = doc.split("\n");
  const middle = lines.indexOf("    return result", 2) + 1;
  const caret = doc.indexOf("    return result", block.length) + 4;
  assert.equal(findLine(doc, "return result", caret), middle);
  const far = "def f():\n    return result\n" + "x".repeat(PREFIX_CHARS + 100) + "\n";
  assert.equal(findLine(far, "return result", far.length), 0);
  assert.equal(nextEditLine(far, { after: "return result", lines: ["    log()"] }, far.length), 0);
});

test("a refusal or a provider failure pauses requests, other answers do not", () => {
  for (const status of [402, 403, 409, 422, 424, 429, 502, 503]) assert.ok(pausesCompletions(status), String(status));
  for (const status of [200, 400, 401, 404, 500]) assert.equal(pausesCompletions(status), false, String(status));
});
