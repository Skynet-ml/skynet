import assert from "node:assert/strict";
import { test } from "node:test";
import {
  budgetResultKind,
  committedLimitFloorCents,
  isBudgetPause,
  isBudgetStop,
  limitPaceCents,
} from "./run-lifecycle.ts";

test("budget stops require a structured terminal reason", () => {
  assert.equal(isBudgetStop({ status: "stopped", stop_reason: "budget_reached" }), true);
  assert.equal(isBudgetStop({ status: "failed", stop_reason: "budget_reached" }), false);
  assert.equal(isBudgetStop({ status: "cancelled", stop_reason: "budget_reached" }), false);
  assert.equal(isBudgetStop({ status: "stopped", stop_reason: "other" }), false);
});

test("budget pauses are paused runs parked by the projection, never by the user", () => {
  assert.equal(isBudgetPause({ status: "paused", stop_reason: "budget_projected" }), true);
  assert.equal(isBudgetPause({ status: "paused", stop_reason: null }), false);
  assert.equal(isBudgetPause({ status: "stopped", stop_reason: "budget_projected" }), false);
  assert.equal(isBudgetStop({ status: "paused", stop_reason: "budget_projected" }), false);
});

test("a seed or score without completed selection never becomes an evaluated result", () => {
  assert.equal(
    budgetResultKind({ terminal_evidence: { candidate_origin: "seed", selection_score: 1 } }),
    "none",
  );
  assert.equal(
    budgetResultKind({
      result_availability: "none",
      terminal_evidence: { candidate_origin: "optimized" },
    }),
    "none",
  );
  assert.equal(
    budgetResultKind({
      result_availability: "evaluated",
      terminal_evidence: { candidate_origin: "seed" },
    }),
    "seed",
  );
  assert.equal(
    budgetResultKind({
      result_availability: "evaluated",
      terminal_evidence: { candidate_origin: "optimized" },
    }),
    "evaluated",
  );
});

// SKY-925: setup checks bill the balance, so setup_spent_cents never counts
// against the limit; run_spent_cents already holds what does.
const ledger = {
  job_id: "job",
  setup_spent_cents: "40",
  run_spent_cents: "12.5",
  reserved_cents: "7",
};

test("the budget block counts run spend and, once a job owns it, the run's holds", () => {
  assert.deepEqual(limitPaceCents(ledger), { spentCents: 12.5, usedCents: 19.5 });
  assert.deepEqual(limitPaceCents({ ...ledger, job_id: null }), {
    spentCents: 12.5,
    usedCents: 12.5,
  });
});

test("the limit to continue sits one cent above run spend and holds, without setup spend", () => {
  assert.equal(committedLimitFloorCents(ledger), 20);
  assert.equal(
    committedLimitFloorCents({ ...ledger, run_spent_cents: "0", reserved_cents: "0" }),
    1,
  );
});
