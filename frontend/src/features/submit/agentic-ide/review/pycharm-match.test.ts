/** Tests for the port of PyCharm's completion matcher and order. */

import assert from "node:assert/strict";
import test from "node:test";

import { arrange, CamelHumpMatcher } from "./pycharm-match.ts";

const matches = (prefix: string, name: string) => new CamelHumpMatcher(prefix).prefixMatches(name);
const order = (prefix: string, names: string[]) => arrange(prefix, names, (n) => n).map((r) => r.item);

test("camel humps match the uppercase letters further on", () => {
  assert.deepEqual(new CamelHumpMatcher("CU").matchingFragments("CurrentUser"), [
    [0, 1],
    [7, 8],
  ]);
  assert.ok(matches("cuUs", "currentUser"));
  assert.ok(matches("gTP", "getTicketPriority"));
});

test("the first letter must match its case, the others need not", () => {
  assert.ok(!matches("cu", "CurrentUser"));
  assert.ok(matches("Cu", "CurrentUser"));
  assert.ok(matches("cURRENT", "current"));
  assert.ok(!matches("Pri", "_private"));
});

test("a middle match needs 3 letters when more of the pattern follows", () => {
  assert.ok(matches("ser", "currentUser"));
  assert.ok(!matches("sx", "currentUser"));
  // The first-letter case rule checks the name's first letter, also for a middle match.
  assert.ok(!matches("ser", "CurrentUser"));
  assert.ok(matches("ticket", "classify_ticket"));
  assert.ok(!matches("ix", "classify"));
  assert.ok(!matches("laT", "classifyTicket"));
  assert.ok(matches("lasT", "classifyTicket"));
});

test("an exact match comes first, then start matches by degree, then middle matches", () => {
  assert.deepEqual(order("pri", ["sprint", "_private", "print", "pri", "PrimaryKey"]), [
    "pri",
    "print",
    "_private",
    "sprint",
  ]);
});

test("names with the same degree sort naturally", () => {
  assert.deepEqual(order("item", ["item10", "item2", "item1"]), ["item1", "item2", "item10"]);
});

test("the same-case exact match comes before another case", () => {
  assert.deepEqual(order("Foo", ["foo", "Foobar", "Foo"]), ["Foo", "Foobar"]);
  assert.deepEqual(order("foo", ["Foo", "foo", "fooBar"]), ["foo", "fooBar"]);
});

test("matched fragments mark the typed letters for highlight", () => {
  const [first] = arrange("clt", ["classify_ticket"], (n) => n);
  assert.deepEqual(first?.fragments, [
    [0, 2],
    [9, 10],
  ]);
});

test("a name picked more recently comes first in its group, but not before an exact match", () => {
  const recent = ["classmethod", "issubclass"];
  const used = (n: string) => (recent.includes(n) ? recent.indexOf(n) : Infinity);
  const ranked = arrange("cl", ["classify", "class", "classmethod", "issubclass", "cl"], (n) => n, used);
  assert.deepEqual(
    ranked.map((r) => r.item),
    ["cl", "classmethod", "class", "classify", "issubclass"],
  );
});
