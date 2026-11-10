import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  formatCount,
  formatCredits,
  formatPercent,
  formatReset,
  formatTime,
} from "./cli-usage.ts";

void describe("cli usage formatting", () => {
  void it("groups counts for the locale", () => {
    assert.equal(formatCount(51234, "en"), "51,234");
  });

  void it("returns null for a time that does not parse", () => {
    assert.equal(formatTime("not a time", "en"), null);
    assert.ok(formatTime("2026-11-06T11:00:00+00:00", "en"));
  });

  void it("formats a percent as a whole number in 0 to 100", () => {
    assert.equal(formatPercent(22.4, "en"), "22%");
    assert.equal(formatPercent(130, "en"), "100%");
    assert.equal(formatPercent(-5, "en"), "0%");
  });

  void it("formats a reset time with its weekday", () => {
    assert.equal(formatReset("bad", "en"), null);
    assert.match(formatReset("2026-11-12T15:00:00+00:00", "en") ?? "", /day/);
  });
});

void describe("formatCredits", () => {
  void it("rounds a credit balance to a whole number", () => {
    assert.equal(formatCredits(2453.717819, "en"), "2,454");
    assert.equal(formatCredits(12.2, "en"), "12");
  });
});
