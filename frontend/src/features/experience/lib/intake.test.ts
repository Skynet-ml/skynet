import { test } from "node:test";
import assert from "node:assert/strict";
import { buildExperiencePatch, emptyIntake, parseIntakeAnswers } from "./intake.ts";

test("buildExperiencePatch finishes the setup with the chosen level and the answers", () => {
  const answers = { ...emptyIntake(), billing: "byok" as const, level: "expert" as const };
  const patch = buildExperiencePatch(answers);
  assert.equal(patch.level, "expert");
  assert.equal(patch.intake_completed, true);
  assert.equal((patch.intake as Record<string, unknown>).billing, "byok");
  assert.equal((patch.intake as Record<string, unknown>).version, 1);
});

test("parseIntakeAnswers round-trips stored answers and ignores a retired email cadence", () => {
  const stored = buildExperiencePatch({
    ...emptyIntake(),
    privacy: "zdr",
    spending_limit_cents: 500,
  }).intake;
  const parsed = parseIntakeAnswers(stored);
  assert.equal(parsed?.privacy, "zdr");
  assert.equal(parsed?.spending_limit_cents, 500);
  // A draft from before the notification inbox still parses; its cadence is dropped.
  const older = parseIntakeAnswers({ notifications: { cadence: "live" }, privacy: "deny" });
  assert.equal(older?.privacy, "deny");
  assert.equal(older && "notifications" in older, false);
  assert.equal(parseIntakeAnswers(null), null);
  assert.equal(parseIntakeAnswers([1]), null);
});

test("a stored CLI subscription answer reads back", () => {
  assert.equal(parseIntakeAnswers({ billing: "subscription" })?.billing, "subscription");
  assert.equal(parseIntakeAnswers({ billing: "other" })?.billing, "platform");
});
