import assert from "node:assert/strict";
import { test } from "node:test";
import type { ExecutionBudget } from "../../../shared/types/execution-budget.ts";
import {
  ExecutionBudgetSession,
  type BudgetSessionDependencies,
  type WizardBudgetDraft,
} from "./execution-budget-session.ts";

const snapshot = (total = 20, revision = 1): ExecutionBudget => ({
  id: "budget-1",
  total_cents: total,
  revision,
  generation: 0,
  state: "open",
  job_id: null,
  setup_spent_cents: "1.25",
  run_spent_cents: "0",
  reserved_cents: "2.50",
  available_cents: String(total - 3.75),
  billed_cents: 2,
  wallet_setup_spent_cents: "1.25",
  wallet_run_spent_cents: "0",
  wallet_reserved_cents: 3,
  account_available_cents: 100,
  external_spent_cents: "0",
  pending_operations: 0,
  blocked_reason: null,
  uncapped: false,
});
function fixture(
  overrides: Partial<BudgetSessionDependencies> = {},
  draft: WizardBudgetDraft = { budgetTotalCents: 20 },
) {
  const saved: WizardBudgetDraft[] = [];
  let nextKey = 0;
  const session = new ExecutionBudgetSession(draft, {
    persist: async (data) => {
      saved.push({ ...data });
    },
    create: async () => snapshot(),
    get: async () => snapshot(),
    update: async (_id, total, revision) => snapshot(total, revision + 1),
    changed: () => {},
    newKey: () => `key-${++nextKey}`,
    ...overrides,
  });
  return { session, saved };
}

test("storage failure warns without blocking valid work and preserves retry identity in memory", async () => {
  let creates = 0;
  const { session } = fixture({
    persist: async () => {
      throw new Error("quota");
    },
    create: async () => {
      creates++;
      return snapshot();
    },
  });
  await session.ensure();
  assert.equal(creates, 1);
  assert.equal(session.persistenceUnavailable, true);
  assert.equal(session.draft.budgetCreateIdempotencyKey, "key-1");
  assert.deepEqual(session.draft.executionBudgetRef, { id: "budget-1", revision: 1 });
});

test("uncertain creation survives refresh with the original key and amount before applying an edited total", async () => {
  const calls: unknown[] = [];
  let saved: WizardBudgetDraft = {};
  const first = fixture({
    persist: async (draft) => {
      saved = { ...draft };
    },
    create: async (total, key) => {
      calls.push([total, key]);
      throw new Error("connection lost");
    },
  }).session;
  await assert.rejects(first.ensure(), /connection lost/);
  assert.equal(saved.budgetCreateIdempotencyKey, "key-1");
  const resumed = fixture(
    {
      create: async (total, key) => {
        calls.push([total, key]);
        return snapshot();
      },
      update: async (_id, total, revision) => {
        calls.push(["update", total, revision]);
        return snapshot(total, revision + 1);
      },
    },
    { ...saved, budgetTotalCents: 30 },
  ).session;
  const result = await resumed.ensure();
  assert.deepEqual(calls, [
    [20, "key-1"],
    [20, "key-1"],
    ["update", 30, 1],
  ]);
  assert.equal(result.available_cents, "26.25");
  assert.deepEqual(resumed.draft.executionBudgetRef, { id: "budget-1", revision: 2 });
});

test("Continue waits for restored server state then applies the desired total with its current revision", async () => {
  let resolve!: (value: ExecutionBudget) => void;
  let reads = 0;
  const { session } = fixture(
    {
      get: async () =>
        ++reads === 1
          ? new Promise((done) => {
              resolve = done;
            })
          : snapshot(20, 3),
    },
    { executionBudgetRef: { id: "budget-1", revision: 1 }, budgetTotalCents: 30 },
  );
  const refresh = session.refresh();
  const ensure = session.ensure();
  resolve(snapshot(20, 3));
  await refresh;
  assert.equal((await ensure).revision, 4);
  assert.equal(session.budget?.total_cents, 30);
});

test("an account or Start new detachment fences late server results", async () => {
  let resolve!: (value: ExecutionBudget) => void;
  const { session, saved } = fixture(
    {
      get: async () =>
        new Promise((done) => {
          resolve = done;
        }),
    },
    { executionBudgetRef: { id: "budget-1", revision: 1 }, budgetTotalCents: 20 },
  );
  const refresh = session.refresh();
  session.detach();
  resolve(snapshot());
  await assert.rejects(refresh, { name: "AbortError" });
  assert.equal(session.budget, null);
  assert.equal(saved.length, 0);
});

test("submission retry retains its key while a different server fingerprint starts a new logical submission", async () => {
  const { session, saved } = fixture();
  const first = await session.submissionKey("fingerprint-a");
  assert.equal(await session.submissionKey("fingerprint-a"), first);
  assert.notEqual(await session.submissionKey("fingerprint-b"), first);
  assert.equal(saved.at(-1)?.submissionFingerprint, "fingerprint-b");
});

test("a rejected decrease restores the accepted total and exposes the funded minimum", async () => {
  const conflict = Object.assign(new Error("budget.conflict"), {
    params: { current_total_cents: 20, minimum_total_cents: 12 },
  });
  let reads = 0;
  const { session, saved } = fixture(
    {
      get: async () => {
        reads += 1;
        return snapshot(20, 1);
      },
      update: async () => {
        throw conflict;
      },
    },
    { executionBudgetRef: { id: "budget-1", revision: 1 }, budgetTotalCents: 5 },
  );

  await assert.rejects(session.ensure(), /budget.conflict/);
  assert.equal(reads, 2);
  assert.equal(session.draft.budgetTotalCents, 20);
  assert.equal(session.budget?.total_cents, 20);
  assert.equal(session.minimumTotalCents, 12);
  assert.equal(saved.at(-1)?.budgetTotalCents, 20);
});

