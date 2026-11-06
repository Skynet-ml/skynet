import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { formatCount, formatTime } from "./cli-usage.ts";

void describe("cli usage formatting", () => {
  void it("groups counts for the locale", () => {
    assert.equal(formatCount(51234, "en"), "51,234");
  });

  void it("returns null for a time that does not parse", () => {
    assert.equal(formatTime("not a time", "en"), null);
    assert.ok(formatTime("2026-11-06T11:00:00+00:00", "en"));
  });
});
