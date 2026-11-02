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

/** The clock time a message was sent, in 12-hour form ("8:25 PM"). */
export function formatMessageTime(ms: number | null | undefined, locale: string): string {
  if (ms == null || !Number.isFinite(ms)) return "";
  return new Intl.DateTimeFormat(locale, {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(ms);
}

/** A quiet gap this long starts a new group under its own time divider. */
const GROUP_GAP_MS = 30 * 60 * 1000;

function dayKey(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/** Whether a message at `ms` opens a new group after one at `prevMs`. */
export function startsTimeGroup(prevMs: number | undefined, ms: number | undefined): boolean {
  if (ms == null) return false;
  if (prevMs == null) return true;
  return ms - prevMs >= GROUP_GAP_MS || dayKey(prevMs) !== dayKey(ms);
}

/**
 * The divider over a group: "Today 8:14 PM", "Yesterday 8:14 PM", or the
 * date and time for older days. `words` turns a time into the today and
 * yesterday phrases, so the caller keeps the catalog lookups.
 */
export function timeDividerLabel(
  ms: number,
  now: number,
  locale: string,
  words: { today: (time: string) => string; yesterday: (time: string) => string },
): string {
  const time = formatMessageTime(ms, locale);
  if (dayKey(ms) === dayKey(now)) return words.today(time);
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (dayKey(ms) === dayKey(yesterday.getTime())) return words.yesterday(time);
  const sameYear = new Date(ms).getFullYear() === new Date(now).getFullYear();
  return new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
    ...(sameYear ? {} : { year: "numeric" }),
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(ms);
}
