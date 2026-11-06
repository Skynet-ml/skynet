import { test } from "node:test";
import assert from "node:assert/strict";
import {
  dayCalls,
  dayWindow,
  formatUsageCallDate,
  formatUsageDay,
  usageDayRows,
} from "./usage-days.ts";

test("formatUsageCallDate shows the call's UTC date, also west of UTC", () => {
  const previous = process.env.TZ;
  process.env.TZ = "America/Los_Angeles";
  try {
    assert.equal(formatUsageCallDate("2026-11-06T01:30:00+00:00", "en-US"), "Nov 6, 2026");
  } finally {
    process.env.TZ = previous;
  }
});

test("formatUsageDay labels the UTC day itself, also west of UTC", () => {
  const previous = process.env.TZ;
  process.env.TZ = "America/Los_Angeles";
  try {
    assert.equal(formatUsageDay("2026-11-06", "en-US"), "Nov 6");
  } finally {
    process.env.TZ = previous;
  }
});

test("usageDayRows lists one row per day, newest first, with cost, calls and tokens", () => {
  const rows = usageDayRows([
    { date: "2026-11-01", billed_cents: 120, runs: 3, input_tokens: 1000, output_tokens: 250 },
    { date: "2026-11-03", billed_cents: 40, runs: 1, input_tokens: 10, output_tokens: 5 },
  ]);
  assert.deepEqual(rows, [
    { date: "2026-11-03", cents: 40, calls: 1, tokens: 15, inputTokens: 10, outputTokens: 5 },
    {
      date: "2026-11-01",
      cents: 120,
      calls: 3,
      tokens: 1250,
      inputTokens: 1000,
      outputTokens: 250,
    },
  ]);
});

test("usageDayRows merges repeated dates and drops days with no billed call", () => {
  const rows = usageDayRows([
    { date: "2026-11-02", billed_cents: 10, runs: 1, input_tokens: 4, output_tokens: 1 },
    { date: "2026-11-02", billed_cents: 15, runs: 2, input_tokens: 6, output_tokens: 2 },
    { date: "2026-11-04", billed_cents: 0, runs: 0 },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].cents, 25);
  assert.equal(rows[0].calls, 3);
  assert.equal(rows[0].tokens, 13);
});

test("usageDayRows keeps calls and tokens null when an older backend sends only cents", () => {
  const [row] = usageDayRows([{ date: "2026-11-02", billed_cents: 30 }]);
  assert.equal(row.tokens, null);
  assert.equal(row.calls, null);
  assert.equal(row.cents, 30);
});

test("dayWindow spans the whole UTC day up to the next midnight inside the range", () => {
  assert.deepEqual(
    dayWindow("2026-11-02", "2026-10-01T00:00:00.000Z", "2026-11-06T10:00:00.000Z"),
    {
      startIso: "2026-11-02T00:00:00.000Z",
      endIso: "2026-11-03T00:00:00.000Z",
    },
  );
});

test("dayWindow clips the first and last day to the range bounds", () => {
  assert.deepEqual(
    dayWindow("2026-11-06", "2026-10-07T08:30:00.000Z", "2026-11-06T10:00:00.000Z"),
    {
      startIso: "2026-11-06T00:00:00.000Z",
      endIso: "2026-11-06T10:00:00.000Z",
    },
  );
  assert.deepEqual(
    dayWindow("2026-10-07", "2026-10-07T08:30:00.000Z", "2026-11-06T10:00:00.000Z"),
    {
      startIso: "2026-10-07T08:30:00.000Z",
      endIso: "2026-10-08T00:00:00.000Z",
    },
  );
});

test("dayCalls keeps only the billed run calls of that day", () => {
  const entries = [
    { id: "a", at: "2026-11-02T20:00:00+00:00", kind: "run", cents: -12 },
    { id: "b", at: "2026-11-02T19:00:00+00:00", kind: "topup", cents: 500 },
    { id: "c", at: "2026-11-02T18:00:00+00:00", kind: "run", cents: 0 },
    { id: "d", at: "2026-11-01T23:00:00+00:00", kind: "run", cents: -3 },
    { id: "e", at: "2026-11-02T01:00:00+00:00", kind: "run", cents: -7 },
  ];
  assert.deepEqual(
    dayCalls(entries, "2026-11-02").map((entry) => entry.id),
    ["a", "e"],
  );
});

test("dayWindow keeps a call in the last millisecond of the day, and dayCalls drops the next midnight", () => {
  const { startIso, endIso } = dayWindow(
    "2026-11-02",
    "2026-10-01T00:00:00.000Z",
    "2026-11-06T10:00:00.000Z",
  );
  const late = "2026-11-02T23:59:59.999500+00:00";
  const midnight = "2026-11-03T00:00:00+00:00";
  // The backend compares at microsecond precision with `created_at <= end`.
  assert.ok(Date.parse(late) <= Date.parse(endIso) && Date.parse(late) >= Date.parse(startIso));
  assert.deepEqual(
    dayCalls(
      [
        { id: "late", at: late, kind: "run", cents: -2 },
        { id: "next", at: midnight, kind: "run", cents: -3 },
      ],
      "2026-11-02",
    ).map((entry) => entry.id),
    ["late"],
  );
});
