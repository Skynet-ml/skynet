/** BM25 ranking, tokenization, and the Enter routing rule of the search boxes. */

import assert from "node:assert/strict";
import test from "node:test";

import { buildBm25Index, isStrongMatch, searchBm25, tokenize } from "./bm25.ts";

test("tokenize lowercases, drops diacritics and vowel points, and splits on punctuation", () => {
  assert.deepEqual(tokenize("Café, API-Key!"), ["cafe", "api", "key"]);
  assert.deepEqual(tokenize("שָׁלוֹם עוֹלָם"), ["שלום", "עולם"]);
  assert.deepEqual(tokenize("  "), []);
});

test("tokenize splits Han and kana runs into character pairs", () => {
  assert.deepEqual(tokenize("設定画面"), ["設定", "定画", "画面"]);
  assert.deepEqual(tokenize("字"), ["字"]);
});

test("a rare word outranks a common one, and a short field outranks a long one", () => {
  const index = buildBm25Index([
    { item: "a", fields: [{ text: "billing invoices" }] },
    { item: "b", fields: [{ text: "billing usage charts runs runs runs runs runs" }] },
    { item: "c", fields: [{ text: "runs dashboard" }] },
  ]);
  assert.deepEqual(
    searchBm25(index, "billing").map((hit) => hit.item),
    ["a", "b"],
  );
  assert.equal(searchBm25(index, "invoices")[0].item, "a");
  assert.equal(searchBm25(index, "nothing").length, 0);
});

test("field weight lifts a title match above a body match", () => {
  const index = buildBm25Index([
    { item: "body", fields: [{ text: "Overview" }, { text: "engines and models" }] },
    { item: "title", fields: [{ text: "Engines", weight: 3 }, { text: "how runs work" }] },
  ]);
  assert.equal(searchBm25(index, "engines")[0].item, "title");
});

test("the last word matches as a prefix while the user types; earlier words do not", () => {
  const index = buildBm25Index([
    { item: "billing", fields: [{ text: "billing" }] },
    { item: "bias", fields: [{ text: "bias" }] },
  ]);
  assert.deepEqual(
    searchBm25(index, "bil").map((hit) => hit.item),
    ["billing"],
  );
  assert.equal(searchBm25(index, "bil bias")[0].matched, 1);
  // One letter is too short to expand.
  assert.equal(searchBm25(index, "b").length, 0);
});

test("a Hebrew one-letter prefix still matches the bare word", () => {
  const index = buildBm25Index([{ item: "account", fields: [{ text: "חשבון משתמש" }] }]);
  assert.equal(searchBm25(index, "החשבון")[0]?.item, "account");
});

test("Enter opens a short full match and sends a sentence or a miss to the agent", () => {
  const index = buildBm25Index([
    { item: "billing", fields: [{ text: "Billing", weight: 3 }, { text: "Add funds and see invoices" }] },
    { item: "keys", fields: [{ text: "API keys", weight: 3 }, { text: "Provider keys for models" }] },
  ]);
  const opens = (query: string) => isStrongMatch(query, searchBm25(index, query)[0]);
  assert.equal(opens("billing"), true);
  assert.equal(opens("api key"), true);
  assert.equal(opens("how do I add funds to my account"), false);
  assert.equal(opens("zzzz"), false);
  assert.equal(isStrongMatch("billing", undefined), false);
});
