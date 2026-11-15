/** Tests for the automatic retry after a model capacity error. */

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import test from "node:test";

import { capacityRetryDelay, secondsLeft } from "./capacity-retry.ts";

test("each capacity failure in a row waits longer, then stops retrying by itself", () => {
  assert.deepEqual([0, 1, 2, 3].map(capacityRetryDelay), [10, 30, 120, 300]);
  assert.equal(capacityRetryDelay(4), null);
  assert.equal(capacityRetryDelay(-1), 10);
});

test("the countdown rounds up and stops at zero", () => {
  assert.equal(secondsLeft(10_000, 0), 10);
  assert.equal(secondsLeft(10_000, 9_001), 1);
  assert.equal(secondsLeft(10_000, 10_000), 0);
  assert.equal(secondsLeft(10_000, 12_000), 0);
});

test("the countdown's accessible name starts with its visible words and has no number", () => {
  const dir = new URL("../../../../../i18n/locales/ui/", import.meta.url);
  let checked = 0;
  for (const file of readdirSync(dir).filter((name) => name.endsWith(".json"))) {
    const catalog = JSON.parse(readFileSync(new URL(file, dir), "utf8")) as Record<string, string>;
    const visible = catalog["shared.agent.retry_in"];
    const name = catalog["shared.agent.retry_auto"];
    if (visible === undefined && name === undefined) continue;
    assert.ok(visible && name, `${file} needs both countdown strings`);
    // Voice control matches the words a user sees before the number (WCAG 2.5.3).
    const before = visible.slice(0, visible.indexOf("{seconds}")).trim();
    assert.ok(name.startsWith(before), `${file}: "${name}" must start with "${before}"`);
    assert.doesNotMatch(name, /\d/, file);
    checked += 1;
  }
  assert.ok(checked >= 17, `checked ${checked} locales`);
});
