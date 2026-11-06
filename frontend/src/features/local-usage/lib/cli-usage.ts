import type { CliProvider } from "@/features/billing";

/** One window's totals, as `GET /local/cli-usage` sends them. */
export interface CliUsageTotals {
  calls: number;
  failed_calls: number;
  runs: number;
  input_tokens: number;
  output_tokens: number;
}

export type CliUsageWindow = "5h" | "7d";

export const CLI_USAGE_WINDOWS: readonly CliUsageWindow[] = ["5h", "7d"];

export interface CliUsageEntry {
  cli: CliProvider;
  last_used_at: string | null;
  cap: { reset_at: string; message: string } | null;
  windows: Record<CliUsageWindow, CliUsageTotals>;
}

/** The read-only report of the CLI calls the user's Skynet runs made. */
export interface CliUsageReport {
  generated_at: string;
  windows: CliUsageWindow[];
  clis: CliUsageEntry[];
}

/** Format a count in the active locale. */
export function formatCount(value: number, locale: string): string {
  return new Intl.NumberFormat(locale).format(value);
}

/** Format a backend ISO time in the active locale, or `null` when it does not parse. */
export function formatTime(iso: string, locale: string): string | null {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return null;
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(time);
}

