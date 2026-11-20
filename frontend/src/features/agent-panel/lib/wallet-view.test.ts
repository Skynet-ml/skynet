import assert from "node:assert/strict";
import { test } from "node:test";

const { walletView } = await import("./wallet-view.ts");

// The Mac app's local grant (settings.local_grant_cents); it must never reach the card.
const LOCAL_GRANT_CENTS = 100_000_000;

test("a web wallet keeps one balance and the usage", () => {
  const view = walletView({
    balance_cents: 700,
    usage: [{ id: "u1", label: "Run", cents: -40 }],
  });
  assert.deepEqual(view, {
    kind: "web",
    totalCents: 700,
    usage: [{ id: "u1", label: "Run", cents: -40 }],
  });
});

test("a linked Mac shows the hosted balance and its low flag", () => {
  assert.deepEqual(walletView({ linked: true, hosted_balance_cents: 1234, low_balance: true }), {
    kind: "hosted",
    totalCents: 1234,
    low: true,
  });
});

test("a Mac with no linked account shows no number", () => {
  const view = walletView({
    linked: false,
    message: "No Skynet account is linked to this Mac; sign in.",
  });
  assert.deepEqual(view, { kind: "not_linked" });
  assert.ok(!JSON.stringify(view).includes(String(LOCAL_GRANT_CENTS)));
});

test("a linked Mac that cannot reach Skynet shows no number", () => {
  assert.deepEqual(walletView({ linked: true, balance_available: false, message: "x" }), {
    kind: "unavailable",
  });
});

test("an unknown or empty result gives no view", () => {
  assert.equal(walletView(undefined), null);
  assert.equal(walletView([]), null);
  assert.equal(walletView({ paid_balance_cents: 5 }), null);
  assert.equal(walletView({ balance_cents: "5" }), null);
});
