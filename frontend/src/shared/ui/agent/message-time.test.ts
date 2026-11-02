/** Tests for the chat message time helpers. */

import assert from "node:assert/strict";
import test from "node:test";

import {
  formatMessageTime,
  parseMessageTime,
  startsTimeGroup,
  timeDividerLabel,
} from "./message-time.ts";

// ICU puts a narrow no-break space before AM/PM; compare as plain spaces.
const plain = (s: string) => s.replace(/[  ]/g, " ");
const at = (y: number, mo: number, d: number, h: number, mi: number) =>
  new Date(y, mo, d, h, mi).getTime();
const words = { today: (t: string) => `Today ${t}`, yesterday: (t: string) => `Yesterday ${t}` };

test("formats a time in 12-hour AM/PM form", () => {
  assert.equal(plain(formatMessageTime(at(2026, 10, 1, 20, 25), "en-US")), "8:25 PM");
  assert.equal(plain(formatMessageTime(at(2026, 10, 1, 0, 5), "en-US")), "12:05 AM");
});

test("keeps the 12-hour form in a 24-hour locale", () => {
  const he = formatMessageTime(at(2026, 10, 1, 20, 25), "he-IL");
  assert.match(he, /8:25/);
  assert.doesNotMatch(he, /20:25/);
});

test("shows no time when the message has none", () => {
  assert.equal(formatMessageTime(undefined, "en-US"), "");
  assert.equal(formatMessageTime(null, "en-US"), "");
  assert.equal(formatMessageTime(Number.NaN, "en-US"), "");
});

test("parses stored times, reading a zoneless time as UTC", () => {
  const utc = Date.UTC(2026, 10, 1, 20, 25);
  assert.equal(parseMessageTime("2026-11-01T20:25:00+00:00"), utc);
  assert.equal(parseMessageTime("2026-11-01T20:25:00Z"), utc);
  assert.equal(parseMessageTime("2026-11-01T20:25:00"), utc);
  assert.equal(parseMessageTime("2026-11-01T22:25:00+02:00"), utc);
  assert.equal(parseMessageTime(undefined), undefined);
  assert.equal(parseMessageTime("not a date"), undefined);
});

test("opens a group on the first timed turn, a long gap, or a new day", () => {
  const t = at(2026, 10, 1, 20, 14);
  assert.equal(startsTimeGroup(undefined, t), true);
  assert.equal(startsTimeGroup(t, t + 5 * 60_000), false);
  assert.equal(startsTimeGroup(t, t + 30 * 60_000), true);
  assert.equal(startsTimeGroup(at(2026, 10, 1, 23, 50), at(2026, 10, 2, 0, 5)), true);
  assert.equal(startsTimeGroup(t, undefined), false);
});

test("labels dividers as today, yesterday, or a date", () => {
  const now = at(2026, 10, 1, 21, 0);
  assert.equal(plain(timeDividerLabel(at(2026, 10, 1, 20, 14), now, "en-US", words)), "Today 8:14 PM");
  assert.equal(
    plain(timeDividerLabel(at(2026, 9, 31, 9, 3), now, "en-US", words)),
    "Yesterday 9:03 AM",
  );
  const older = plain(timeDividerLabel(at(2026, 9, 28, 9, 3), now, "en-US", words));
  assert.match(older, /Oct 28/);
  assert.match(older, /9:03 AM/);
  assert.doesNotMatch(older, /2026/);
  assert.match(plain(timeDividerLabel(at(2025, 9, 28, 9, 3), now, "en-US", words)), /2025/);
});
