import { test } from "node:test";
import assert from "node:assert/strict";
import {
  centsToUsd,
  totalCents,
  walletStatus,
  formatCents,
  formatCentsUsd,
  formatBudgetUsd,
  formatUsd,
  formatResetDate,
  EMPTY_WALLET,
  DEFAULT_PRICING_TERMS,
  type WalletBalance,
} from "./wallet.ts";

/** Assert two floats match within a tolerance — cents→USD is fractional. */
function near(actual: number, expected: number): void {
  assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} ≈ ${expected}`);
}

const walletWith = (paidBalanceCents: number, freeRemaining: number): WalletBalance => ({
  ...EMPTY_WALLET,
  paidBalanceCents,
  freeGrant: { ...EMPTY_WALLET.freeGrant, centsRemaining: freeRemaining },
});

test("empty wallet never seeds demo balances or activity", () => {
  assert.equal(EMPTY_WALLET.paidBalanceCents, 0);
  assert.equal(EMPTY_WALLET.freeGrant.centsRemaining, 0);
  assert.deepEqual(EMPTY_WALLET.usage, []);
});

test("default pricing has no BYOK model fee", () => {
  assert.equal(DEFAULT_PRICING_TERMS.byokFeeFraction, 0);
});

test("centsToUsd values each cent at $0.01", () => {
  near(centsToUsd(100), 1);
  near(centsToUsd(250), 2.5);
  near(centsToUsd(0), 0);
});

test("totalCents sums the free grant remaining and the purchased balance", () => {
  assert.equal(totalCents(walletWith(1240, 480)), 1720);
  assert.equal(totalCents(walletWith(0, 0)), 0);
});

test("walletStatus buckets the wallet by spendable value", () => {
  assert.equal(walletStatus(walletWith(0, 0)), "empty");
  // 30 cents = $0.30, under the $0.50 low-balance line.
  assert.equal(walletStatus(walletWith(30, 0)), "low");
  assert.equal(walletStatus(walletWith(1000, 0)), "healthy");
});

test("formatCents groups thousands in the given locale", () => {
  assert.equal(formatCents(1240, "en-US"), "1,240");
  assert.equal(formatCents(0, "en-US"), "0");
});

test("formatUsd keeps sub-cent precision but two decimals otherwise", () => {
  assert.equal(formatUsd(1, "en-US"), "$1.00");
  assert.equal(formatUsd(0, "en-US"), "$0.00");
  assert.equal(formatUsd(0.003, "en-US"), "$0.003");
});

test("formatCentsUsd renders a cent balance in dollars", () => {
  assert.equal(formatCentsUsd(4512, "en-US"), "$45.12");
  assert.equal(formatCentsUsd(500, "en-US"), "$5.00");
  assert.equal(formatCentsUsd(0, "en-US"), "$0.00");
});

test("formatBudgetUsd renders a decimal cent string in dollars", () => {
  assert.equal(formatBudgetUsd("116.06994", "en-US"), "$1.16");
  assert.equal(formatBudgetUsd("2000", "en-US"), "$20.00");
  // A fraction of a cent keeps sub-cent precision instead of collapsing to $0.00.
  assert.equal(formatBudgetUsd("0.4", "en-US"), "$0.004");
});

test("formatResetDate renders the reset date in the given locale", () => {
  assert.match(formatResetDate("2026-07-01T12:00:00Z", "en-US"), /2026/);
});
