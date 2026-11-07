import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { HARDENING_ERROR_UNKNOWN_KEY, hardeningErrorKey } from "./hardening-error.ts";

// The codes `backend/core/worker/hardening_runner.py` stores in `job.error`.
const RUNNER_CODES = [
  "hardening.no_scorer",
  "hardening.no_target",
  "hardening.no_python_metric",
  "hardening.no_managed_models",
  "hardening.byok_missing",
  "hardening.byok_not_served",
  "hardening.runner_failed",
];

function catalog(locale: string): Record<string, string> {
  const url = new URL(`../../../../../i18n/locales/ui/${locale}.json`, import.meta.url);
  return JSON.parse(readFileSync(url, "utf8")) as Record<string, string>;
}

test("each runner code maps to its own key", () => {
  const keys = RUNNER_CODES.map(hardeningErrorKey);
  assert.deepEqual(
    keys,
    RUNNER_CODES.map((code) => code.replace("hardening.", "submit.v2.harden.error.")),
  );
  assert.equal(new Set(keys).size, RUNNER_CODES.length);
});

test("any other error maps to the generic key", () => {
  for (const error of ["hardening.something_new", "RuntimeError", "", "toString", "__proto__"]) {
    assert.equal(hardeningErrorKey(error), HARDENING_ERROR_UNKNOWN_KEY);
  }
});

test("every key has a Hebrew and an English sentence", () => {
  const he = catalog("he");
  const en = catalog("en");
  for (const key of [...RUNNER_CODES.map(hardeningErrorKey), HARDENING_ERROR_UNKNOWN_KEY]) {
    assert.match(he[key] ?? "", /[֐-׿]/, `he: ${key}`);
    assert.ok(en[key]?.trim(), `en: ${key}`);
    assert.doesNotMatch(en[key], /[֐-׿]/, `en: ${key}`);
  }
});
