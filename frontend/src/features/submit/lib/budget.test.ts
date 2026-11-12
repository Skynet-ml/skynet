import assert from "node:assert/strict";
import { test } from "node:test";

import { availableBudget, suggestedRunName } from "./budget.ts";

test("availableBudget subtracts setup, run and reserved spend and never goes negative", () => {
  assert.equal(availableBudget({ total: null, setupSpent: 3, runSpent: 0, reserved: 0 }), null);
  assert.equal(availableBudget({ total: 100, setupSpent: 3, runSpent: 20, reserved: 10 }), 67);
  assert.equal(availableBudget({ total: 5, setupSpent: 9, runSpent: 0, reserved: 0 }), 0);
});

test("suggestedRunName takes the first sentence and trims punctuation", () => {
  assert.equal(suggestedRunName(""), "");
  assert.equal(
    suggestedRunName("  Short, accurate answers. Cite the source.  "),
    "Short, accurate answers",
  );
  assert.equal(suggestedRunName("Improve recall\nsecond line"), "Improve recall");
  assert.equal(suggestedRunName("תשובות קצרות ומדויקות. עם מקורות."), "תשובות קצרות ומדויקות");
});

test("suggestedRunName cuts long sentences at a word boundary", () => {
  const long =
    "Make the assistant answer customer billing questions accurately and politely every time";
  const name = suggestedRunName(long, 40);
  assert.ok(name.length <= 41, name);
  assert.ok(name.endsWith("…"), name);
  assert.equal(name, "Make the assistant answer customer…");
});
