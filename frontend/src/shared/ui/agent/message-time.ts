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
const WEEK = 7 * DAY;

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

/** The shared relative-time formatter for this locale and numeric style. */
function relativeFormat(
  locale: string,
  numeric: Intl.RelativeTimeFormatNumeric,
): Intl.RelativeTimeFormat {
  const key = `relative|${locale}|${numeric}`;
  let format = formatters.get(key) as Intl.RelativeTimeFormat | undefined;
  if (!format) {
    format = new Intl.RelativeTimeFormat(locale, { numeric });
    formatters.set(key, format);
  }
  return format;
}

/**
 * How long ago a message was sent: "now", "5 minutes ago", "3 days ago".
 * From a week on it is the day and month ("Nov 7"), with the year only
 * when it is not this year.
 */
export function formatMessageAge(
  ms: number | null | undefined,
  now: number,
  locale: string,
): string {
  if (ms == null || !Number.isFinite(ms)) return "";
  const age = Math.max(0, now - ms);
  if (age >= WEEK) {
    const sameYear = new Date(ms).getFullYear() === new Date(now).getFullYear();
    return dateFormat(locale, {
      month: "short",
      day: "numeric",
      ...(sameYear ? {} : { year: "numeric" }),
    }).format(ms);
  }
  const relative = relativeFormat(locale, "auto");
  if (age < MINUTE) return relative.format(0, "second");
  if (age < HOUR) return relative.format(-Math.floor(age / MINUTE), "minute");
  if (age < DAY) return relative.format(-Math.floor(age / HOUR), "hour");
  return relativeFormat(locale, "always").format(
    -Math.floor(age / DAY),
    "day",
  );
}

/** The date and time a message was sent, for its tooltip: "Nov 12, 2026, 3:04 PM". */
export function formatMessageDate(ms: number, locale: string): string {
  return dateFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(ms);
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
    date = relativeFormat(locale, "auto").format(-days, "day");
    date = date.charAt(0).toLocaleUpperCase(locale) + date.slice(1);
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
