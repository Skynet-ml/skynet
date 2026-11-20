/**
 * Read a stored message time (ISO string from the backend) as epoch ms.
 * A time with no zone is UTC: SQLite drops the offset that Postgres keeps.
 */
export function parseMessageTime(value: string | null | undefined): number | undefined {
  if (!value) return undefined;
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const ms = Date.parse(zoned);
  return Number.isFinite(ms) ? ms : undefined;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

// A long chat formats every message time on each minute tick; building an
// Intl formatter is slow, so each locale and option set builds one, once.
const formatters = new Map<string, Intl.DateTimeFormat | Intl.RelativeTimeFormat>();

/** The shared date formatter for this locale and these options. */
function dateFormat(locale: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `date|${locale}|${JSON.stringify(options)}`;
  let format = formatters.get(key) as Intl.DateTimeFormat | undefined;
  if (!format) {
    format = new Intl.DateTimeFormat(locale, options);
    formatters.set(key, format);
  }
  return format;
}

/** The shared relative-time formatter for this locale and these options. */
function relativeFormat(
  locale: string,
  options: Intl.RelativeTimeFormatOptions,
): Intl.RelativeTimeFormat {
  const key = `relative|${locale}|${JSON.stringify(options)}`;
  let format = formatters.get(key) as Intl.RelativeTimeFormat | undefined;
  if (!format) {
    format = new Intl.RelativeTimeFormat(locale, options);
    formatters.set(key, format);
  }
  return format;
}

// The units of a recent message's age, largest first, in seconds (the Claude app's table).
const AGE_UNITS: ReadonlyArray<[Intl.RelativeTimeFormatUnit, number]> = [
  ["year", 31_536_000],
  ["month", 2_592_000],
  ["week", 604_800],
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
];

const ageStyles = new Map<string, Intl.RelativeTimeFormatStyle>();

/**
 * The narrow style, or the short one where the narrow style writes a past
 * time with a leading sign ("-5 min"), as the Claude app does.
 */
function ageStyle(locale: string): Intl.RelativeTimeFormatStyle {
  let style = ageStyles.get(locale);
  if (!style) {
    const narrow = relativeFormat(locale, { numeric: "always", style: "narrow" });
    const signed = AGE_UNITS.some(([unit]) =>
      /^[+\u2212\p{Pd}]/u.test(narrow.format(-5, unit).trim()),
    );
    style = signed ? "short" : "narrow";
    ageStyles.set(locale, style);
  }
  return style;
}

/** A recent message's age: "now", "5m ago", "1h ago". */
function formatAge(ms: number, now: number, locale: string): string {
  const style = ageStyle(locale);
  const seconds = Math.round((ms - now) / 1000);
  const size = Math.abs(seconds);
  const format = relativeFormat(locale, { numeric: "auto", style });
  if (size < 60) return format.format(0, "second");
  const [unit, unitSeconds] = AGE_UNITS.find(([, s]) => size >= s) ?? ["minute", 60];
  return format.format(Math.round(seconds / unitSeconds), unit);
}

// The Claude app shows the age only up to 58.5 minutes, and from one minute in the future.
const RECENT_MS = 3_510_000;

/** The visible label, tooltip and screen-reader date of a message time. */
export interface MessageTimeText {
  label: string;
  tooltip: string;
  spoken: string;
}

/**
 * The texts of a message time, by the Claude app's rule. A recent message
 * shows its age ("5m ago") with the date and time as the tooltip. An older
 * one shows its time ("3:04 PM") with the full date and time as the tooltip.
 *
 * Args:
 *   ms: When the message was sent, in epoch ms.
 *   now: The current time, in epoch ms.
 *   locale: The Intl locale.
 *
 * Returns:
 *   The texts, or null when the time is unknown.
 */
export function messageTimeText(
  ms: number | null | undefined,
  now: number,
  locale: string,
): MessageTimeText | null {
  if (ms == null || !Number.isFinite(ms)) return null;
  const age = now - ms;
  if (age > -MINUTE && age < RECENT_MS) {
    return {
      label: formatAge(ms, now, locale),
      tooltip: dateFormat(locale, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      }).format(ms),
      spoken: dateFormat(locale, { dateStyle: "full", timeStyle: "short" }).format(ms),
    };
  }
  return {
    label: dateFormat(locale, { hour: "numeric", minute: "numeric" }).format(ms),
    tooltip: dateFormat(locale, { dateStyle: "full", timeStyle: "medium" }).format(ms),
    spoken: dateFormat(locale, { dateStyle: "full" }).format(ms),
  };
}

// One shared minute clock for every message time on the page, so a long chat
// runs one timer, not one for each message.
const clockListeners = new Set<() => void>();
let clockNow = Date.now();
let clockTimer: ReturnType<typeof setInterval> | null = null;

/** Subscribe to the minute clock; the first listener starts its one timer, the last stops it. */
export function subscribeMinuteClock(listener: () => void): () => void {
  clockListeners.add(listener);
  if (!clockTimer) {
    clockNow = Date.now();
    clockTimer = setInterval(() => {
      clockNow = Date.now();
      for (const notify of clockListeners) notify();
    }, MINUTE);
  }
  return () => {
    clockListeners.delete(listener);
    if (clockListeners.size === 0 && clockTimer) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  };
}

/** The minute clock's time. It stays the same between ticks, as a store snapshot must. */
export function minuteClockNow(): number {
  // With no timer running, the value can be old; it moves on at most once a minute.
  if (!clockTimer && Date.now() - clockNow >= MINUTE) clockNow = Date.now();
  return clockNow;
}

/** One message's place in the separator rule: who sent it, and when. */
export interface SeparatorStamp {
  role: "user" | "agent";
  at?: number;
}

/**
 * Which turns open with a time separator, and at which time. A turn gets one
 * when its first user message is more than an hour old, or after a reply
 * when the next user message comes more than an hour later, or the next
 * reply more than ten minutes later. This is the rule Codex uses.
 */
export function separatorTimes(
  turns: ReadonlyArray<readonly SeparatorStamp[]>,
  now: number,
): Array<number | null> {
  let previous: SeparatorStamp | null = null;
  let seenUser = false;
  return turns.map((stamps) => {
    let at: number | null = null;
    for (const stamp of stamps) {
      if (at === null && stamp.at != null && Number.isFinite(stamp.at)) {
        const firstUser = stamp.role === "user" && !seenUser;
        const gap = stamp.role === "user" ? HOUR : 10 * MINUTE;
        if (
          (firstUser && now - stamp.at > HOUR) ||
          (previous?.role === "agent" && previous.at != null && stamp.at - previous.at > gap)
        ) {
          at = stamp.at;
        }
      }
      seenUser = seenUser || stamp.role === "user";
      previous = stamp;
    }
    return at;
  });
}

/** Whole calendar days from `ms` to `now`, in local time. */
function calendarDays(ms: number, now: number): number {
  const a = new Date(ms);
  const b = new Date(now);
  return Math.round(
    (Date.UTC(b.getFullYear(), b.getMonth(), b.getDate()) -
      Date.UTC(a.getFullYear(), a.getMonth(), a.getDate())) /
      DAY,
  );
}

/**
 * The parts of a time separator: "Today" or "Yesterday", the weekday within a
 * week, the short date within a year, then the date with its year. `older`
 * is true from a week on, where the label reads "{date} at {time}".
 */
export function separatorParts(
  ms: number,
  now: number,
  locale: string,
): { date: string; time: string; older: boolean } {
  const days = Math.max(calendarDays(ms, now), 0);
  let date: string;
  if (days <= 1) {
    const format = relativeFormat(locale, { numeric: "auto" });
    date = format.format(-days, "day");
    // Codex capitalizes "today" and "yesterday" in English only.
    if (locale.startsWith("en")) {
      date = date.charAt(0).toUpperCase() + date.slice(1);
    }
  } else if (days <= 7) {
    date = dateFormat(locale, { weekday: "long" }).format(ms);
  } else if (days <= 365) {
    date = dateFormat(locale, { weekday: "short", month: "short", day: "numeric" }).format(ms);
  } else {
    date = dateFormat(locale, { month: "short", day: "numeric", year: "numeric" }).format(ms);
  }
  const time = dateFormat(locale, { hour: "numeric", minute: "2-digit" }).format(ms);
  return { date, time, older: days > 7 };
}
