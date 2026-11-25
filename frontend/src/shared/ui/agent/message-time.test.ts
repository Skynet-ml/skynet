/** Tests for the chat message time helpers. */

import assert from "node:assert/strict";
import test from "node:test";

import {
  formatMessageAge,
  formatMessageDate,
  minuteClockNow,
  parseMessageTime,
  separatorParts,
  separatorTimes,
  subscribeMinuteClock,
} from "./message-time.ts";

const at = (y: number, mo: number, d: number, h: number, mi: number) =>
  new Date(y, mo, d, h, mi).getTime();
const now = at(2026, 10, 12, 14, 42);
const MIN = 60_000;

test("shows a recent message as how long ago it was sent", () => {
  assert.equal(formatMessageAge(now - 20_000, now, "en-US"), "now");
  assert.equal(formatMessageAge(now - 5 * MIN, now, "en-US"), "5 minutes ago");
  assert.equal(formatMessageAge(now - 3 * 60 * MIN, now, "en-US"), "3 hours ago");
  assert.equal(formatMessageAge(now - 26 * 60 * MIN, now, "en-US"), "1 day ago");
  assert.equal(formatMessageAge(now - 6 * 24 * 60 * MIN, now, "en-US"), "6 days ago");
});

test("shows the day and month from a week on, and the year only for other years", () => {
  assert.equal(formatMessageAge(at(2026, 10, 5, 9, 3), now, "en-US"), "Nov 5");
  assert.equal(formatMessageAge(at(2025, 10, 5, 9, 3), now, "en-US"), "Nov 5, 2025");
});

test("never shows a future time", () => {
  assert.equal(formatMessageAge(now + 5 * MIN, now, "en-US"), "now");
});

test("shows no time when the message has none", () => {
  assert.equal(formatMessageAge(undefined, now, "en-US"), "");
  assert.equal(formatMessageAge(null, now, "en-US"), "");
  assert.equal(formatMessageAge(Number.NaN, now, "en-US"), "");
});

test("gives the short date and time for the tooltip", () => {
  const full = formatMessageDate(at(2026, 10, 7, 14, 42), "en-US").replace(/[\u202f\u00a0]/g, " ");
  assert.equal(full, "Nov 7, 2026, 2:42 PM");
});

test("builds each Intl formatter once and reuses it", () => {
  const real = { date: Intl.DateTimeFormat, relative: Intl.RelativeTimeFormat };
  let built = 0;
  class CountedDate extends real.date {
    constructor(...args: ConstructorParameters<typeof Intl.DateTimeFormat>) {
      super(...args);
      built += 1;
    }
  }
  class CountedRelative extends real.relative {
    constructor(...args: ConstructorParameters<typeof Intl.RelativeTimeFormat>) {
      super(...args);
      built += 1;
    }
  }
  Object.assign(Intl, { DateTimeFormat: CountedDate, RelativeTimeFormat: CountedRelative });
  try {
    const formatAll = () => [
      formatMessageAge(now - 5 * MIN, now, "fr-CA"),
      formatMessageAge(now - 3 * 24 * 60 * MIN, now, "fr-CA"),
      formatMessageAge(at(2026, 10, 5, 9, 3), now, "fr-CA"),
      formatMessageDate(now, "fr-CA"),
    ];
    const first = formatAll();
    assert.equal(built, 4);
    assert.deepEqual(formatAll(), first);
    assert.equal(built, 4);
  } finally {
    Object.assign(Intl, { DateTimeFormat: real.date, RelativeTimeFormat: real.relative });
  }
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

test("every message time shares one minute timer", () => {
  const real = { set: globalThis.setInterval, clear: globalThis.clearInterval };
  const timers: Array<() => void> = [];
  let cleared = 0;
  Object.assign(globalThis, {
    setInterval: (fn: () => void) => timers.push(fn),
    clearInterval: () => {
      cleared += 1;
    },
  });
  try {
    let ticks = 0;
    const stopA = subscribeMinuteClock(() => (ticks += 1));
    const stopB = subscribeMinuteClock(() => (ticks += 1));
    assert.equal(timers.length, 1);
    const before = minuteClockNow();
    assert.equal(minuteClockNow(), before);
    timers[0]!();
    assert.equal(ticks, 2);
    stopA();
    assert.equal(cleared, 0);
    stopB();
    assert.equal(cleared, 1);
    subscribeMinuteClock(() => {})();
    assert.equal(timers.length, 2);
  } finally {
    Object.assign(globalThis, { setInterval: real.set, clearInterval: real.clear });
  }
});

const HOUR = 60 * MIN;

test("opens an old chat with a time separator, and a fresh one without", () => {
  assert.deepEqual(separatorTimes([[{ role: "user", at: now - 2 * HOUR }]], now), [now - 2 * HOUR]);
  assert.deepEqual(separatorTimes([[{ role: "user", at: now - 5 * MIN }]], now), [null]);
});

test("adds a separator after an hour's pause before the user, or ten minutes before a reply", () => {
  const t0 = now - 30 * MIN;
  const turns = [
    [{ role: "user" as const, at: t0 }, { role: "agent" as const, at: t0 + MIN }],
    [{ role: "user" as const, at: t0 + 20 * MIN }, { role: "agent" as const, at: t0 + 21 * MIN }],
  ];
  assert.deepEqual(separatorTimes(turns, now), [null, null]);
  const later = [turns[0]!, [{ role: "user" as const, at: t0 + MIN + HOUR + 1 }]];
  assert.deepEqual(separatorTimes(later, now + 2 * HOUR), [t0, t0 + MIN + HOUR + 1]);
  const replyLater = [[{ role: "agent" as const, at: t0 }], [{ role: "agent" as const, at: t0 + 11 * MIN }]];
  assert.deepEqual(separatorTimes(replyLater, now), [null, t0 + 11 * MIN]);
});

test("names the day: today, a weekday, a short date, then a date with its year", () => {
  const clean = (s: string) => s.replace(/[\u202f\u00a0]/g, " ");
  const p = (ms: number) => {
    const r = separatorParts(ms, now, "en-US");
    return [r.date, clean(r.time), r.older];
  };
  assert.deepEqual(p(at(2026, 10, 12, 9, 5)), ["Today", "9:05 AM", false]);
  assert.deepEqual(p(at(2026, 10, 11, 9, 5)), ["Yesterday", "9:05 AM", false]);
  assert.deepEqual(p(at(2026, 10, 9, 9, 5)), ["Monday", "9:05 AM", false]);
  assert.deepEqual(p(at(2026, 9, 1, 9, 5)), ["Thu, Oct 1", "9:05 AM", true]);
  assert.deepEqual(p(at(2025, 9, 1, 9, 5)), ["Oct 1, 2025", "9:05 AM", true]);
});
