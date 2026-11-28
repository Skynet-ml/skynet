import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_LEVEL,
  defaultOpen,
  detailTabGate,
  isVisible,
  jobsColumnGate,
  normalizeLevel,
} from "./abstraction.ts";

test("normalizeLevel maps unknown, null and missing values to expert and keeps a chosen level", () => {
  assert.equal(DEFAULT_LEVEL, "expert");
  assert.equal(normalizeLevel("guided"), "guided");
  assert.equal(normalizeLevel("standard"), "standard");
  assert.equal(normalizeLevel("expert"), "expert");
  assert.equal(normalizeLevel(null), "expert");
  assert.equal(normalizeLevel(undefined), "expert");
  assert.equal(normalizeLevel("beginner"), "expert");
});

test("Guided hides the advanced wizard surfaces; Standard and Expert show them", () => {
  for (const surface of [
    "wizard.split",
    "wizard.optimizer_settings",
    "wizard.model_params",
    "wizard.shinka_settings",
  ] as const) {
    assert.equal(isVisible(surface, "guided"), false, surface);
    assert.equal(isVisible(surface, "standard"), true, surface);
    assert.equal(isVisible(surface, "expert"), true, surface);
  }
});

test("only Expert opens disclosures by default", () => {
  for (const surface of [
    "wizard.optimizer_settings",
    "wizard.model_params",
    "wizard.shinka_settings",
    "logs.verbose",
  ] as const) {
    assert.equal(defaultOpen(surface, "expert"), true, surface);
    assert.equal(defaultOpen(surface, "standard"), false, surface);
    assert.equal(defaultOpen(surface, "guided"), false, surface);
  }
});

test("run-page tabs: Guided drops Logs, Data, Code and Config but keeps the rest", () => {
  const guided = detailTabGate("guided");
  for (const tab of ["logs", "data", "code", "config"]) assert.equal(guided(tab), false, tab);
  for (const tab of ["overview", "artifact", "usage", "unknown-tab"]) {
    assert.equal(guided(tab), true, tab);
  }
  const expert = detailTabGate("expert");
  for (const tab of ["logs", "data", "code", "config", "overview"]) assert.equal(expert(tab), true);
});

test("jobs columns: Guided keeps name, status, created and score", () => {
  const guided = jobsColumnGate("guided");
  for (const key of ["name", "status", "created_at", "optimized_test_metric"])
    assert.equal(guided(key), true, key);
  for (const key of ["optimization_id", "elapsed_seconds"]) {
    assert.equal(guided(key), false, key);
  }
  assert.equal(jobsColumnGate("standard")("elapsed_seconds"), true);
});
