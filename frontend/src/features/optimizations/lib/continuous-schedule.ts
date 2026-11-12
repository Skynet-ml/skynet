/*
 * Pure helpers for a continuous plan's schedule editor: one-click presets,
 * the checks that mirror the backend's ScheduleRule, and the plain-language
 * summary ("Every weekday at 09:00") as a message key with its parameters.
 */

import type { ScheduleFrequency, ScheduleRule } from "@/shared/types/continuous";

/** Longest interval per frequency; matches `MAX_EVERY` in `continuous_schedule.py`. */
export const MAX_EVERY: Record<ScheduleFrequency, number> = {
  hourly: 12,
  daily: 30,
  weekly: 8,
  monthly: 12,
};

export const FREQUENCIES: readonly ScheduleFrequency[] = ["hourly", "daily", "weekly", "monthly"];

/** Monday = 0, as the backend counts. */
const WEEKDAYS: readonly number[] = [0, 1, 2, 3, 4, 5, 6];
const MONDAY_TO_FRIDAY = [0, 1, 2, 3, 4];

interface WeekInfo {
  firstDay: number;
  weekend: number[];
}

/** The locale's week (ISO days, Monday = 1), or null where the engine can't say. */
function weekInfo(locale: string): WeekInfo | null {
  try {
    const maximized = new Intl.Locale(locale).maximize() as Intl.Locale & {
      getWeekInfo?: () => WeekInfo;
      weekInfo?: WeekInfo;
    };
    return maximized.getWeekInfo?.() ?? maximized.weekInfo ?? null;
  } catch {
    return null;
  }
}

/** The locale's working days (Sunday to Thursday in Israel), Monday = 0, sorted. */
export function workweek(locale: string): number[] {
  const weekend = weekInfo(locale)?.weekend;
  if (!weekend?.length) return MONDAY_TO_FRIDAY;
  return WEEKDAYS.filter((day) => !weekend.includes(day + 1));
}

/** Weekdays in the order the locale's calendar shows them. */
export function weekdayOrder(locale: string): number[] {
  const first = (weekInfo(locale)?.firstDay ?? 1) - 1;
  return WEEKDAYS.map((offset) => (first + offset) % 7);
}

export type SchedulePreset = "hourly" | "daily" | "weekdays" | "weekly" | "monthly" | "custom";
export const PRESETS: readonly SchedulePreset[] = [
  "hourly",
  "daily",
  "weekdays",
  "weekly",
  "monthly",
  "custom",
];

/** A weekly check on Monday at 09:00 in the viewer's zone. */
export function defaultRule(timezone: string): ScheduleRule {
  return { frequency: "weekly", every: 1, time: "09:00", weekdays: [0], month_day: 1, timezone };
}

/** The rule a preset gives, keeping the time and zone the owner already picked. */
export function applyPreset(
  rule: ScheduleRule,
  preset: SchedulePreset,
  locale: string,
): ScheduleRule {
  const base = { ...rule, every: 1 };
  switch (preset) {
    case "hourly":
      return { ...base, frequency: "hourly" };
    case "daily":
      return { ...base, frequency: "daily" };
    case "weekdays":
      return { ...base, frequency: "weekly", weekdays: workweek(locale) };
    case "weekly":
      return {
        ...base,
        frequency: "weekly",
        weekdays: rule.weekdays.length === 1 ? rule.weekdays : [0],
      };
    case "monthly":
      return { ...base, frequency: "monthly" };
    default:
      return rule;
  }
}

function sameDays(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((day, i) => day === b[i]);
}

/** Which preset a rule matches, or `custom`. */
export function presetOf(rule: ScheduleRule, locale: string): SchedulePreset {
  if (rule.every !== 1) return "custom";
  if (rule.frequency === "weekly") {
    if (sameDays(rule.weekdays, workweek(locale))) return "weekdays";
    return "weekly";
  }
  return rule.frequency;
}

/** Add or remove a weekday, keeping the list sorted. */
export function toggleWeekday(days: number[], day: number): number[] {
  return days.includes(day) ? days.filter((d) => d !== day) : [...days, day].sort((a, b) => a - b);
}

export type ScheduleProblem =
  | "continuous_tab.schedule.problem.weekday"
  | "continuous_tab.schedule.problem.every"
  | "continuous_tab.schedule.problem.day"
  | "continuous_tab.schedule.problem.time";

/** Why a rule can't be saved, as a UI message key; null when it can. */
export function scheduleProblem(rule: ScheduleRule): ScheduleProblem | null {
  if (!Number.isInteger(rule.every) || rule.every < 1 || rule.every > MAX_EVERY[rule.frequency]) {
    return "continuous_tab.schedule.problem.every";
  }
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(rule.time)) return "continuous_tab.schedule.problem.time";
  if (rule.frequency === "weekly" && rule.weekdays.length === 0) {
    return "continuous_tab.schedule.problem.weekday";
  }
  if (
    rule.frequency === "monthly" &&
    (!Number.isInteger(rule.month_day) || rule.month_day < 1 || rule.month_day > 31)
  ) {
    return "continuous_tab.schedule.problem.day";
  }
  return null;
}

/** Name each weekday in the active locale, Monday first. */
export function weekdayNames(locale: string, width: "narrow" | "short" | "long"): string[] {
  const format = new Intl.DateTimeFormat(locale, { weekday: width, timeZone: "UTC" });
  // 2024-01-01 was a Monday.
  return WEEKDAYS.map((day) => format.format(new Date(Date.UTC(2024, 0, 1 + day))));
}

/** Render an HH:MM wall-clock time in the active locale's style (09:00 or 9:00 AM). */
export function formatClock(time: string, locale: string): string {
  const [hour, minute] = time.split(":").map(Number);
  return new Intl.DateTimeFormat(locale, { timeStyle: "short", timeZone: "UTC" }).format(
    new Date(Date.UTC(2024, 0, 1, hour, minute)),
  );
}

export interface ScheduleSummary {
  key:
    | "continuous_tab.schedule.summary.hourly"
    | "continuous_tab.schedule.summary.daily"
    | "continuous_tab.schedule.summary.weekdays"
    | "continuous_tab.schedule.summary.weekly"
    | "continuous_tab.schedule.summary.monthly";
  params: Record<string, string | number>;
}

/** The rule in words, as a message key and its parameters. */
export function scheduleSummary(rule: ScheduleRule, locale: string): ScheduleSummary {
  const time = formatClock(rule.time, locale);
  const every = rule.every;
  switch (rule.frequency) {
    case "hourly":
      return {
        key: "continuous_tab.schedule.summary.hourly",
        params: { every, minute: rule.time.slice(3) },
      };
    case "daily":
      return { key: "continuous_tab.schedule.summary.daily", params: { every, time } };
    case "weekly": {
      if (every === 1 && sameDays(rule.weekdays, workweek(locale))) {
        return { key: "continuous_tab.schedule.summary.weekdays", params: { time } };
      }
      const names = weekdayNames(locale, rule.weekdays.length > 2 ? "short" : "long");
      const days = new Intl.ListFormat(locale, { style: "long", type: "conjunction" }).format(
        rule.weekdays.map((day) => names[day]!),
      );
      return { key: "continuous_tab.schedule.summary.weekly", params: { every, days, time } };
    }
    default:
      return {
        key: "continuous_tab.schedule.summary.monthly",
        params: { every, day: rule.month_day, time },
      };
  }
}

/** The viewer's own IANA zone, falling back to UTC. */
export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** Every zone the browser knows, with `current` first so it is always selectable. */
export function timeZoneOptions(current: string): string[] {
  const supported =
    typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  const zones = supported.includes("UTC") ? supported : ["UTC", ...supported];
  return zones.includes(current) ? zones : [current, ...zones];
}

/** A calendar date with no time or zone, held as UTC midnight so day arithmetic is exact. */
export function calendarDay(year: number, month: number, day: number): Date {
  return new Date(Date.UTC(year, month, day));
}

/** Today's date in a time zone, so the calendar opens on the plan's own today. */
export function todayIn(timezone: string): Date {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  return calendarDay(get("year"), get("month") - 1, get("day"));
}

/** Monday = 0, matching the rule's weekday numbers. */
export function weekdayOf(day: Date): number {
  return (day.getUTCDay() + 6) % 7;
}

const DAY_MS = 86_400_000;

function daysBetween(from: Date, to: Date): number {
  return Math.round((to.getTime() - from.getTime()) / DAY_MS);
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** A calendar date as `YYYY-MM-DD`. */
function dateKey(day: Date): string {
  return `${day.getUTCFullYear()}-${pad(day.getUTCMonth() + 1)}-${pad(day.getUTCDate())}`;
}

function slotsOn(slots: string[] | undefined, day: Date): Set<string> {
  const prefix = `${dateKey(day)}T`;
  return new Set((slots ?? []).filter((s) => s.startsWith(prefix)).map((s) => s.slice(prefix.length)));
}

/**
 * The HH:MM times a rule checks on one date, counted from `anchor` the way
 * the worker counts them (`_day_times` in `continuous_schedule.py`), with
 * single checks turned off or added on that date.
 */
export function checkTimesOn(rule: ScheduleRule, day: Date, anchor: Date): string[] {
  const skipped = slotsOn(rule.skip, day);
  const added = slotsOn(rule.extra, day);
  const repeat = repeatTimesOn(rule, day, anchor).filter((t) => !skipped.has(t));
  return [...new Set([...repeat, ...added])].sort();
}

/** The HH:MM times the repeat alone checks on one date. */
function repeatTimesOn(rule: ScheduleRule, day: Date, anchor: Date): string[] {
  const [hour = 0, minute = 0] = rule.time.split(":").map(Number);
  const at = [`${pad(hour)}:${pad(minute)}`];
  const offset = daysBetween(anchor, day);
  const mod = (n: number) => ((n % rule.every) + rule.every) % rule.every;
  if (rule.frequency === "hourly") {
    const times: string[] = [];
    for (let h = 0; h < 24; h++) if (mod(offset * 24 + h - hour) === 0) times.push(`${pad(h)}:${pad(minute)}`);
    return times;
  }
  if (rule.frequency === "daily") return mod(offset) === 0 ? at : [];
  if (rule.frequency === "weekly") {
    const anchorMonday = daysBetween(calendarDay(1970, 0, 5), anchor) - weekdayOf(anchor);
    const week = Math.floor((daysBetween(calendarDay(1970, 0, 5), day) - anchorMonday) / 7);
    return rule.weekdays.includes(weekdayOf(day)) && mod(week) === 0 ? at : [];
  }
  const months = (day.getUTCFullYear() - anchor.getUTCFullYear()) * 12 + day.getUTCMonth() - anchor.getUTCMonth();
  if (mod(months) !== 0) return [];
  const last = new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth() + 1, 0)).getUTCDate();
  return day.getUTCDate() === Math.min(rule.month_day, last) ? at : [];
}

/** Sorted, unique slots from `today` on, so turned-off past checks don't pile up. */
function keepSlots(slots: Iterable<string>, today: Date): string[] {
  const from = dateKey(today);
  return [...new Set(slots)].filter((s) => s >= from).sort();
}

/**
 * The rule after one check slot (a date and an HH:MM time) is clicked: a
 * check the repeat makes is turned off for that slot only, a turned-off one
 * comes back, and a slot the repeat skips gets a single added check.
 */
export function toggleSlot(rule: ScheduleRule, day: Date, time: string, today: Date): ScheduleRule {
  const slot = `${dateKey(day)}T${time}`;
  const skip = new Set(rule.skip ?? []);
  const extra = new Set(rule.extra ?? []);
  if (repeatTimesOn(rule, day, today).includes(time)) {
    if (skip.has(slot)) skip.delete(slot);
    else skip.add(slot);
  } else if (extra.has(slot)) {
    extra.delete(slot);
  } else {
    extra.add(slot);
  }
  return { ...rule, skip: keepSlots(skip, today), extra: keepSlots(extra, today) };
}

/**
 * The rule after a date is clicked in the calendar: that date's checks turn
 * off, or a date with none gets one at the rule's time. Weekdays and other
 * dates stay as they are. Hourly rules pick single hours in a clock instead.
 */
export function pickDay(rule: ScheduleRule, day: Date, today: Date): ScheduleRule {
  if (rule.frequency === "hourly") return rule;
  const times = checkTimesOn(rule, day, today);
  if (times.length === 0) {
    return toggleSlot(rule, day, rule.time, today);
  }
  return times.reduce((next, time) => toggleSlot(next, day, time, today), rule);
}

/** The rule after a weekday is clicked: every day but that one, or that day toggled. */
export function pickWeekday(rule: ScheduleRule, weekday: number): ScheduleRule {
  if (rule.frequency === "hourly" || rule.frequency === "monthly") return rule;
  const days = rule.frequency === "daily" ? [...WEEKDAYS] : rule.weekdays;
  const every = rule.frequency === "daily" ? 1 : rule.every;
  return { ...rule, frequency: "weekly", every, weekdays: toggleWeekday(days, weekday) };
}
