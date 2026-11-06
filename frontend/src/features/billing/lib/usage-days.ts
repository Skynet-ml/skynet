/**
 * Day rows for the Remote usage list: one row per UTC day with its cost,
 * call count and tokens, plus the one-day window that loads its calls.
 */

/** One `by_day` item of `GET /billing/usage`. Older backends send only date and cents. */
export interface UsageDayInput {
  date: string;
  billed_cents: number;
  runs?: number;
  input_tokens?: number;
  output_tokens?: number;
}

/** One rendered day row. `calls` and `tokens` are null when the backend did not send them. */
export interface UsageDayRow {
  date: string;
  cents: number;
  calls: number | null;
  tokens: number | null;
  inputTokens: number;
  outputTokens: number;
}

/** The fields of a usage entry that the day helpers read. */
interface DayEntry {
  at: string;
  kind: string;
  cents: number;
}

/** Turn the per-day series into rows, newest day first. Days with no billed call drop out. */
export function usageDayRows(byDay: readonly UsageDayInput[]): UsageDayRow[] {
  const merged = new Map<string, UsageDayRow>();
  for (const day of byDay) {
    const row = merged.get(day.date) ?? {
      date: day.date,
      cents: 0,
      calls: null,
      tokens: null,
      inputTokens: 0,
      outputTokens: 0,
    };
    row.cents += day.billed_cents;
    if (day.runs != null) row.calls = (row.calls ?? 0) + day.runs;
    if (day.input_tokens != null || day.output_tokens != null) {
      row.inputTokens += day.input_tokens ?? 0;
      row.outputTokens += day.output_tokens ?? 0;
      row.tokens = row.inputTokens + row.outputTokens;
    }
    merged.set(day.date, row);
  }
  return [...merged.values()]
    .filter((row) => row.cents > 0 || (row.calls ?? 0) > 0)
    .sort((a, b) => b.date.localeCompare(a.date));
}

/**
 * The short label of one `YYYY-MM-DD` UTC day. It formats in UTC, because the
 * browser's own zone would show the day before west of UTC.
 */
export function formatUsageDay(date: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${date}T00:00:00Z`));
}

/**
 * The UTC bounds of one `YYYY-MM-DD` day, clipped to the selected range so the
 * drill-down never shows calls that the day row did not count.
 */
export function dayWindow(
  date: string,
  rangeStartIso: string,
  rangeEndIso: string,
): { startIso: string; endIso: string } {
  const dayStart = `${date}T00:00:00.000Z`;
  const dayEnd = `${date}T23:59:59.999Z`;
  const startMs = Math.max(Date.parse(dayStart), Date.parse(rangeStartIso));
  const endMs = Math.min(Date.parse(dayEnd), Date.parse(rangeEndIso));
  return { startIso: new Date(startMs).toISOString(), endIso: new Date(endMs).toISOString() };
}

/** The billed run calls of one UTC day, in the order the entries arrive (newest first). */
export function dayCalls<T extends DayEntry>(entries: readonly T[], date: string): T[] {
  return entries.filter(
    (entry) => entry.kind === "run" && entry.cents < 0 && entry.at.slice(0, 10) === date,
  );
}
