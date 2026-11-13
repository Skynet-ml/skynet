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
