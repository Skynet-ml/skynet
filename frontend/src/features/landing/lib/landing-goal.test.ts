import assert from "node:assert/strict";
import { test } from "node:test";

import { saveLandingGoal, takeLandingGoal } from "./landing-goal.ts";

function memoryStorage() {
  const items = new Map<string, string>();
  return {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => void items.set(key, value),
    removeItem: (key: string) => void items.delete(key),
    size: () => items.size,
  };
}

test("a saved goal is taken once, trimmed", () => {
  const storage = memoryStorage();
  saveLandingGoal("  Speed up my build  ", storage);
  assert.equal(takeLandingGoal(storage), "Speed up my build");
  assert.equal(takeLandingGoal(storage), "");
  assert.equal(storage.size(), 0);
});

test("an empty goal clears an older one", () => {
  const storage = memoryStorage();
  saveLandingGoal("Raise my eval score", storage);
  saveLandingGoal("   ", storage);
  assert.equal(takeLandingGoal(storage), "");
});

test("a long paste is capped", () => {
  const storage = memoryStorage();
  saveLandingGoal("x".repeat(10_000), storage);
  assert.equal(takeLandingGoal(storage).length, 4000);
});

test("no storage and a throwing storage both read as no goal", () => {
  assert.equal(takeLandingGoal(null), "");
  const broken = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
    removeItem: () => {},
  };
  saveLandingGoal("goal", broken);
  assert.equal(takeLandingGoal(broken), "");
});

test("a goal older than an hour is dropped and cleared", () => {
  const storage = memoryStorage();
  saveLandingGoal("Cut my API latency", storage, 0);
  assert.equal(takeLandingGoal(storage, 60 * 60 * 1000 + 1), "");
  assert.equal(storage.size(), 0);
  saveLandingGoal("Cut my API latency", storage, 0);
  assert.equal(takeLandingGoal(storage, 60 * 60 * 1000), "Cut my API latency");
});

test("a malformed stored value reads as no goal and is cleared", () => {
  const storage = memoryStorage();
  for (const raw of ["plain text", "{}", '{"goal":1,"savedAt":0}', '{"goal":"x"}']) {
    storage.setItem("skynet.landing-goal", raw);
    assert.equal(takeLandingGoal(storage), "", raw);
    assert.equal(storage.size(), 0, raw);
  }
});
