import assert from "node:assert/strict";
import { test } from "node:test";

import { countFrom, loadRuns, mergeRuns, runKey, sumCounts } from "./merge-runs.ts";

const run = (optimization_id: string, created_at: string | null) => ({
  optimization_id,
  created_at,
});

test("merge tags every run and orders both sides newest first", () => {
  const local = { items: [run("a", "2026-11-03T10:00:00Z"), run("b", "2026-11-01T10:00:00Z")], total: 5 };
  const hosted = { items: [run("c", "2026-11-04T10:00:00Z"), run("d", "2026-11-02T10:00:00Z")], total: 7 };
  const merged = mergeRuns(local, hosted);
  assert.deepEqual(
    merged.items.map((r) => `${r.source}:${r.optimization_id}`),
    ["hosted:c", "local:a", "hosted:d", "local:b"],
  );
  assert.equal(merged.total, 12);
});

test("without hosted the local order and total stay as sent", () => {
  const local = { items: [run("b", "2026-11-01T10:00:00Z"), run("a", "2026-11-03T10:00:00Z")], total: 2, limit: 20 };
  const merged = mergeRuns(local, null);
  assert.deepEqual(merged.items.map((r) => r.optimization_id), ["b", "a"]);
  assert.ok(merged.items.every((r) => r.source === "local"));
  assert.equal(merged.total, 2);
  assert.equal(merged.limit, 20);
});

test("colliding IDs from the two sides keep unique keys", () => {
  const merged = mergeRuns(
    { items: [run("same", "2026-11-01T10:00:00Z")], total: 1 },
    { items: [run("same", "2026-11-02T10:00:00Z")], total: 1 },
  );
  const keys = merged.items.map(runKey);
  assert.deepEqual(keys, ["hosted:same", "local:same"]);
  assert.equal(new Set(keys).size, 2);
  assert.equal(runKey({ optimization_id: "x" }), "local:x");
});

test("runs without a valid date sort last", () => {
  const merged = mergeRuns(
    { items: [run("old", null)], total: 1 },
    { items: [run("new", "2026-11-02T10:00:00Z")], total: 1 },
  );
  assert.deepEqual(merged.items.map((r) => r.optimization_id), ["new", "old"]);
});

test("countFrom counts the loaded runs of each side", () => {
  const items = [{ ...run("a", null), source: "hosted" as const }, run("b", null), run("c", null)];
  assert.equal(countFrom(items, "local"), 2);
  assert.equal(countFrom(items, "hosted"), 1);
});

test("a hosted failure leaves the local runs", async () => {
  const merged = await loadRuns(async (source) => {
    if (source === "hosted") throw new Error("503");
    return { items: [run("a", null)], total: 1 };
  }, true);
  assert.deepEqual(merged.items.map(runKey), ["local:a"]);
  assert.equal(merged.total, 1);
});

test("hosted is not asked when it is off", async () => {
  const asked: string[] = [];
  await loadRuns(async (source) => {
    asked.push(source);
    return { items: [], total: 0 };
  }, false);
  assert.deepEqual(asked, ["local"]);
});

test("a local failure still throws", async () => {
  await assert.rejects(
    loadRuns(async (source) => {
      if (source === "local") throw new Error("down");
      return { items: [], total: 0 };
    }, true),
  );
});

test("sumCounts adds the numeric fields", () => {
  assert.deepEqual(
    sumCounts({ total: 3, running: 1, shared: 2 }, { total: 4, running: 0, stopped: 1 }),
    { total: 7, running: 1, shared: 2, stopped: 1 },
  );
  const local = { total: 1 };
  assert.equal(sumCounts(local, null), local);
});
