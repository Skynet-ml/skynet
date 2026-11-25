/** Tests for the chat message time helpers. */

import assert from "node:assert/strict";
import test from "node:test";

import {
  messageTimeText,
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

const plain = (text: string) => text.replace(/[\u202f\u00a0]/g, " ");

test("shows a message under 58.5 minutes old as its narrow age (the Claude app's rule)", () => {
  const age = (ms: number) => messageTimeText(ms, now, "en-US")?.label;
  assert.equal(age(now - 20_000), "now");
  assert.equal(age(now - 59_000), "now");
  assert.equal(age(now - 90_000), "1m ago");
  assert.equal(age(now - 91_000), "2m ago");
  assert.equal(age(now - 5 * MIN), "5m ago");
  assert.equal(age(now - 58 * MIN), "58m ago");
  assert.equal(age(now + 30_000), "now");
});

test("gives a recent message the short date and time as its tooltip", () => {
  const text = messageTimeText(now - 5 * MIN, now, "en-US")!;
  assert.equal(plain(text.tooltip), "Nov 12, 2026, 2:37 PM");
  assert.equal(plain(text.spoken), "Thursday, November 12, 2026 at 2:37 PM");
});

test("shows an older or future message as its time, with the full date as its tooltip", () => {
  for (const ms of [now - 59 * MIN, at(2025, 10, 5, 9, 3) + 4_000, now + 2 * MIN]) {
    const text = messageTimeText(ms, now, "en-US")!;
    const d = new Date(ms);
    assert.equal(
      plain(text.label),
      plain(d.toLocaleTimeString("en-US", { hour: "numeric", minute: "numeric" })),
    );
  }
  const old = messageTimeText(at(2025, 10, 5, 9, 3) + 4_000, now, "en-US")!;
  assert.equal(plain(old.tooltip), "Wednesday, November 5, 2025 at 9:03:04 AM");
  assert.equal(old.spoken, "Wednesday, November 5, 2025");
});

test("shows no time when the message has none", () => {
  assert.equal(messageTimeText(undefined, now, "en-US"), null);
  assert.equal(messageTimeText(null, now, "en-US"), null);
  assert.equal(messageTimeText(Number.NaN, now, "en-US"), null);
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
      messageTimeText(now - 5 * MIN, now, "fr-CA"),
      messageTimeText(at(2026, 10, 5, 9, 3), now, "fr-CA"),
    ];
    const first = formatAll();
    const once = built;
    assert.ok(once > 0);
    assert.deepEqual(formatAll(), first);
    assert.equal(built, once);
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

test("capitalizes the day name in English only, as Codex does", () => {
  assert.equal(separatorParts(at(2026, 10, 12, 9, 5), now, "en-GB").date, "Today");
  const fr = new Intl.RelativeTimeFormat("fr", { numeric: "auto" }).format(0, "day");
  assert.equal(separatorParts(at(2026, 10, 12, 9, 5), now, "fr").date, fr);
});
