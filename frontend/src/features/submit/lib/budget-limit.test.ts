import assert from "node:assert/strict";
import { test } from "node:test";

const { budgetContinueBlock, budgetShortfall, runBalance } = await import("./budget-limit.ts");

const floor = 119;

test("a capped run falls short under the floor and is clear at it", () => {
  const capped = (limit: number | null) =>
    budgetShortfall(floor, { uncapped: false, limit, balance: null });

  assert.deepEqual(capped(floor - 1), { kind: "limit", needed: floor });
  assert.equal(capped(floor), null);
  assert.equal(capped(null), null);
});

test("the balance has to fund the same floor, capped or not", () => {
  const short = (uncapped: boolean, balance: number | null) =>
    budgetShortfall(floor, { uncapped, limit: floor + 500, balance });

  assert.deepEqual(short(true, floor - 1), { kind: "balance", needed: floor });
  assert.deepEqual(short(false, floor - 1), { kind: "balance", needed: floor });
  assert.equal(short(true, floor), null);
  assert.equal(short(false, null), null);
});

test("a short limit is named before a short balance", () => {
  assert.deepEqual(budgetShortfall(floor, { uncapped: false, limit: 100, balance: 0 }), {
    kind: "limit",
    needed: floor,
  });
});

test("a fractional floor rounds up to the cent the server holds", () => {
  assert.deepEqual(budgetShortfall(118.2, { uncapped: false, limit: 118, balance: null }), {
    kind: "limit",
    needed: 119,
  });
});

test("Continue holds on the limit screens and at the gate, and nowhere else", () => {
  const block = (screen: string, choice: object, gate = false) =>
    budgetContinueBlock(
      screen,
      floor,
      { uncapped: false, limit: null, balance: null, ...choice },
      gate,
    );

  assert.deepEqual(block("budget", { limit: 100 }), { kind: "limit", needed: floor });
  assert.deepEqual(block("plan", { limit: 100 }), { kind: "limit", needed: floor });
  assert.equal(block("budget", { limit: floor }), null);
  assert.equal(block("models", { limit: 100 }), null);
  assert.deepEqual(block("models", { limit: 100 }, true), { kind: "limit", needed: floor });
  assert.deepEqual(block("cases", { uncapped: true, balance: 5 }, true), {
    kind: "balance",
    needed: floor,
  });
  assert.equal(block("budget", { uncapped: true }), null);
  assert.equal(
    budgetContinueBlock("budget", null, { uncapped: false, limit: 1, balance: 0 }, true),
    null,
  );
});

test("a Web run is funded by the hosted balance, a Mac run by the Mac wallet", () => {
  const mac = { available: true, totalCents: 5000 };
  const web = { available: true, totalCents: 40 };

  assert.equal(runBalance("local", mac, web), 5000);
  assert.equal(runBalance("hosted", mac, web), 40);
  assert.equal(runBalance("hosted", mac, { available: false, totalCents: 0 }), null);
  assert.equal(runBalance("local", { available: false, totalCents: 0 }, web), null);
});
