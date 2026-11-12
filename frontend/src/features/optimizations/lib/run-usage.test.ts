import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  availableGroupings,
  groupUsage,
  hasProviderSpend,
  UNATTRIBUTED,
  usageTotal,
  type RunUsageRow,
} from "./run-usage.ts";

const row = (over: Partial<RunUsageRow>): RunUsageRow => ({
  role: "task",
  model: "openai/gpt-5",
  stage: null,
  candidate: null,
  billing: "skynet",
  charged_cents: 0,
  provider_cents: null,
  calls: 0,
  input_tokens: 0,
  output_tokens: 0,
  latency_ms_total: 0,
  latency_calls: 0,
  unpriced_calls: 0,
  pending_calls: 0,
  ...over,
});

const rows: RunUsageRow[] = [
  row({ role: "proposer", candidate: "c1", charged_cents: 5, calls: 2, latency_ms_total: 300, latency_calls: 2 }),
  row({ role: "proposer", candidate: null, charged_cents: 1, calls: 1 }),
  row({ role: "scorer", model: "openai/gpt-5-mini", candidate: "c1", charged_cents: 2, calls: 4 }),
  row({ role: "sandbox", model: null, charged_cents: 10 }),
  row({ role: "setup", model: null, charged_cents: 0.5 }),
];

describe("groupUsage", () => {
  it("keeps non-model costs as their own rows under every grouping", () => {
    for (const grouping of ["role", "model", "stage", "candidate"] as const) {
      const groups = groupUsage(rows, grouping);
      const nonModel = groups.filter((g) => g.nonModel).map((g) => g.key);
      assert.deepEqual(nonModel.sort(), ["sandbox", "setup"]);
      const sum = groups.reduce((s, g) => s + g.chargedCents, 0);
      assert.equal(sum, usageTotal(rows).chargedCents);
    }
  });

  it("keeps the rounding row out of model groupings and in the total", () => {
    const rounded = [...rows, row({ role: "rounding", model: null, charged_cents: 0.5 })];
    const groups = groupUsage(rounded, "model");
    assert.ok(groups.find((g) => g.key === "rounding")?.nonModel);
    assert.equal(usageTotal(rounded).chargedCents, 19);
    assert.deepEqual(availableGroupings([row({ role: "rounding", model: null })]), ["role"]);
  });

  it("groups a call by its kind before its stage", () => {
    const groups = groupUsage(
      [
        row({ stage: "training", kind: "mutation", calls: 3 }),
        row({ stage: "training", kind: "meta_notes", calls: 1 }),
        row({ stage: "evaluation", calls: 2 }),
      ],
      "stage",
    );
    assert.deepEqual(groups.map((g) => g.key).sort(), ["evaluation", "meta_notes", "mutation"]);
  });

  it("puts model spend without a tag under unattributed, after attributed groups", () => {
    const groups = groupUsage(rows, "candidate");
    assert.deepEqual(
      groups.map((g) => g.key),
      ["c1", UNATTRIBUTED, "sandbox", "setup"],
    );
    assert.equal(groups[0].chargedCents, 7);
    assert.equal(groups[0].calls, 6);
  });

  it("averages latency only over calls that recorded one", () => {
    const [proposer] = groupUsage(rows, "role");
    assert.equal(proposer.key, "proposer");
    assert.equal(proposer.avgLatencyMs, 150);
    const scorer = groupUsage(rows, "role").find((g) => g.key === "scorer");
    assert.equal(scorer?.avgLatencyMs, null);
  });

  it("marks a group unpriced only when every call lacked a price", () => {
    const groups = groupUsage(
      [row({ calls: 2, unpriced_calls: 2 }), row({ model: "x/y", calls: 2, unpriced_calls: 1 })],
      "model",
    );
    assert.equal(groups.find((g) => g.key === "openai/gpt-5")?.unpriced, true);
    assert.equal(groups.find((g) => g.key === "x/y")?.unpriced, false);
  });

  it("sums provider charges and leaves them null when no row has one", () => {
    const withByok = [row({ billing: "byok", provider_cents: 3 }), row({ billing: "byok", provider_cents: 4 })];
    assert.equal(groupUsage(withByok, "role")[0].providerCents, 7);
    assert.equal(groupUsage(rows, "role")[0].providerCents, null);
    assert.equal(hasProviderSpend(withByok), true);
    assert.equal(hasProviderSpend(rows), false);
  });
});

describe("availableGroupings", () => {
  it("offers only groupings the run recorded", () => {
    assert.deepEqual(availableGroupings(rows), ["role", "model", "candidate"]);
    assert.deepEqual(availableGroupings([row({ model: null }), row({ role: "sandbox", stage: "x" })]), ["role"]);
  });
});

describe("apiRateCents", () => {
  it("sums subscription API-rate amounts apart from the charge", () => {
    const sub = [
      row({ role: "task", billing: "subscription", calls: 3, api_rate_cents: 1.25 }),
      row({ role: "proposer", billing: "subscription", calls: 1, api_rate_cents: 0.5 }),
    ];
    const total = usageTotal(sub);
    assert.equal(total.apiRateCents, 1.75);
    assert.equal(total.chargedCents, 0);
    assert.deepEqual(
      groupUsage(sub, "role").map((g) => [g.key, g.apiRateCents]),
      [
        ["proposer", 0.5],
        ["task", 1.25],
      ],
    );
  });

  it("stays null when no row has an API-rate amount", () => {
    assert.equal(usageTotal(rows).apiRateCents, null);
    const unpriced = row({ billing: "subscription", calls: 2, api_rate_cents: null });
    assert.equal(usageTotal([unpriced]).apiRateCents, null);
  });
});
