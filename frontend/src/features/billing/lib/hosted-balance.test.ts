import { test } from "node:test";
import assert from "node:assert/strict";
import {
  hostedBalanceReader,
  loadHostedBalance,
  shownBalance,
  type HostedBalance,
} from "./hosted-balance.ts";

import { EMPTY_WALLET, totalCents, walletStatus, type WalletBalance } from "./wallet.ts";

const failWith = (status: number) => () =>
  Promise.reject(Object.assign(new Error("x"), { status }));

const hostedWallet = (paid: number, grant = 0): WalletBalance => ({
  ...EMPTY_WALLET,
  paidBalanceCents: paid,
  freeGrant: { centsRemaining: grant, centsTotal: grant },
});

const api = (wallet: () => Promise<WalletBalance>, linked = async () => true) => ({
  linked,
  wallet,
  total: totalCents,
  status: walletStatus,
});

test("an unlinked Mac asks for a link and never reads a wallet", async () => {
  let read = false;
  const shown = await loadHostedBalance(
    api(
      async () => {
        read = true;
        return hostedWallet(1);
      },
      async () => false,
    ),
  );
  assert.deepEqual(shown, { kind: "link" });
  assert.equal(read, false);
});

test("a linked Mac shows the web total: paid balance plus grant, with the web status", async () => {
  // SKY-340: the old desktop balance route subtracted the holds of running
  // jobs, so the Mac showed less than the web for the same account.
  const wallet = hostedWallet(1000, 234);
  assert.deepEqual(await loadHostedBalance(api(async () => wallet)), {
    kind: "hosted",
    cents: totalCents(wallet),
    status: walletStatus(wallet),
    wallet,
  });
  assert.equal(totalCents(wallet), 1234);
  for (const [paid, status] of [
    [1234, "healthy"],
    [40, "low"],
    [0, "empty"],
  ] as const) {
    const shown = await loadHostedBalance(api(async () => hostedWallet(paid)));
    assert.equal(shown.kind === "hosted" && shown.status, status, String(paid));
  }
});

test("a revoked link asks for a new link", async () => {
  for (const status of [401, 409]) {
    assert.deepEqual(await loadHostedBalance(api(failWith(status))), { kind: "link" });
  }
});

test("a hosted or local failure is unavailable, never the local credit", async () => {
  assert.deepEqual(await loadHostedBalance(api(failWith(502))), { kind: "unavailable" });
  assert.deepEqual(await loadHostedBalance(api(failWith(500), failWith(500))), {
    kind: "unavailable",
  });
});

const localCredit = {
  loading: false,
  available: true,
  totalCents: 100_000_000,
  status: "healthy" as const,
};

test("the web shows its wallet unchanged", () => {
  assert.deepEqual(shownBalance(null, localCredit), {
    ...localCredit,
    linkNeeded: false,
    hosted: false,
  });
});

test("the Mac app never shows the local credit", () => {
  assert.deepEqual(shownBalance(
      { kind: "hosted", cents: 1234, status: "healthy", wallet: hostedWallet(1234) },
      localCredit,
    ), {
    loading: false,
    available: true,
    totalCents: 1234,
    status: "healthy",
    linkNeeded: false,
    hosted: true,
  });
  for (const kind of ["loading", "link", "unavailable"] as const) {
    const shown = shownBalance({ kind }, localCredit);
    assert.equal(shown.available, false, kind);
    assert.equal(shown.totalCents, 0, kind);
    assert.equal(shown.loading, kind === "loading", kind);
    assert.equal(shown.linkNeeded, kind === "link", kind);
  }
});

function deferred() {
  let resolve!: (b: HostedBalance) => void;
  const promise = new Promise<HostedBalance>((r) => (resolve = r));
  return { promise, resolve };
}

test("only the newest read sets the balance", async () => {
  const seen: HostedBalance[] = [];
  const slow = deferred();
  const fast = deferred();
  const loads = [slow.promise, fast.promise];
  const read = hostedBalanceReader(
    () => loads.shift()!,
    (b) => seen.push(b),
  );
  const first = read(true);
  const second = read(true);
  fast.resolve({ kind: "link" });
  await second;
  slow.resolve({ kind: "hosted", cents: 5, status: "healthy", wallet: hostedWallet(5) });
  await first;
  assert.deepEqual(seen, [{ kind: "link" }]);
});

test("a focus read waits 30 s after a linked balance; a forced read does not", async () => {
  let now = 0;
  let calls = 0;
  const read = hostedBalanceReader(
    async () => {
      calls += 1;
      return { kind: "hosted", cents: 5, status: "healthy", wallet: hostedWallet(5) };
    },
    () => {},
    () => now,
  );
  await read(false);
  now = 29_000;
  await read(false);
  assert.equal(calls, 1);
  await read(true);
  assert.equal(calls, 2);
  now = 60_000;
  await read(false);
  assert.equal(calls, 3);
});

test("a focus read never waits while the Mac is unlinked", async () => {
  let calls = 0;
  const read = hostedBalanceReader(
    async () => {
      calls += 1;
      return { kind: "link" };
    },
    () => {},
    () => 0,
  );
  await read(false);
  await read(false);
  assert.equal(calls, 2);
});
