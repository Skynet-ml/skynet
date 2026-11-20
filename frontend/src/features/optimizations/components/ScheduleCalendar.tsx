"use client";

import { useMemo, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { CaretLeft, CaretRight } from "@/shared/ui/icons";
import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import type { ScheduleRule } from "@/shared/types/continuous";
import { cn } from "@/shared/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/primitives/popover";
import {
  calendarDay,
  checkTimesOn,
  formatClock,
  pickDay,
  toggleSlot,
  pickWeekday,
  todayIn,
  weekdayNames,
  weekdayOf,
  weekdayOrder,
} from "../lib/continuous-schedule";

/** Six rows cover every month, so the grid keeps its height while paging. */
const CELLS = 42;

/**
 * A month calendar that both shows and sets the schedule: lit dates are the
 * check days, a click on a date turns that one date on or off (on an hourly
 * rule it opens a clock to pick that date's hours), a weekday heading
 * changes the repeat, and hovering a date reads out its check times.
 */
export function ScheduleCalendar({
  value,
  onChange,
}: {
  value: ScheduleRule;
  onChange: (rule: ScheduleRule) => void;
}) {
  const locale = getActiveIntlLocale();
  const reduceMotion = useReducedMotion() ?? false;
  const today = useMemo(() => todayIn(value.timezone), [value.timezone]);
  const [month, setMonth] = useState(() =>
    calendarDay(today.getUTCFullYear(), today.getUTCMonth(), 1),
  );
  const [direction, setDirection] = useState(0);
  const [hovered, setHovered] = useState<Date | null>(null);
  const [clockDay, setClockDay] = useState<string | null>(null);
  const order = useMemo(() => weekdayOrder(locale), [locale]);
  const narrowDays = useMemo(() => weekdayNames(locale, "narrow"), [locale]);
  const longDays = useMemo(() => weekdayNames(locale, "long"), [locale]);

  const hourly = value.frequency === "hourly";
  const weekdayPickable = value.frequency === "daily" || value.frequency === "weekly";
  const atStart =
    month.getUTCFullYear() === today.getUTCFullYear() &&
    month.getUTCMonth() === today.getUTCMonth();

  const cells = useMemo(() => {
    const lead = order.indexOf(weekdayOf(month));
    return Array.from({ length: CELLS }, (_, i) =>
      calendarDay(month.getUTCFullYear(), month.getUTCMonth(), 1 - lead + i),
    );
  }, [month, order]);

  const page = (step: number) => {
    setDirection(step);
    setMonth((m) => calendarDay(m.getUTCFullYear(), m.getUTCMonth() + step, 1));
  };

  const title = new Intl.DateTimeFormat(locale, {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(month);
  const dayLabel = (day: Date) =>
    new Intl.DateTimeFormat(locale, {
      weekday: "long",
      month: "long",
      day: "numeric",
      timeZone: "UTC",
    }).format(day);

  const readout = (() => {
    if (!hovered) return null;
    const times = hovered < today ? [] : checkTimesOn(value, hovered, today);
    const when =
      times.length === 0
        ? msg("continuous_tab.schedule.calendar.no_check")
        : times.length === 1
          ? formatClock(times[0] ?? value.time, locale)
          : formatMsg("continuous_tab.schedule.calendar.checks", { count: times.length });
    return `${dayLabel(hovered)} · ${when}`;
  })();

  const slide = reduceMotion ? 0 : 24;

  return (
    <div className="select-none">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm font-semibold" aria-live="polite">
          {title}
        </p>
        <div className="flex gap-1">
          {[
            {
              step: -1,
              icon: CaretLeft,
              label: msg("continuous_tab.schedule.calendar.prev"),
              disabled: atStart,
            },
            {
              step: 1,
              icon: CaretRight,
              label: msg("continuous_tab.schedule.calendar.next"),
              disabled: false,
            },
          ].map(({ step, icon: Icon, label, disabled }) => (
            <button
              key={step}
              type="button"
              aria-label={label}
              disabled={disabled}
              onClick={() => page(step)}
              className="grid size-8 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-background hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45 disabled:pointer-events-none disabled:opacity-30"
            >
              <Icon className="size-3.5 rtl:-scale-x-100" />
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-7 gap-1 pb-1">
        {order.map((weekday) => {
          const on =
            value.frequency === "daily" ||
            (value.frequency === "weekly" && value.weekdays.includes(weekday));
          return (
            <button
              key={weekday}
              type="button"
              disabled={!weekdayPickable}
              aria-pressed={weekdayPickable ? on : undefined}
              aria-label={longDays[weekday]}
              onClick={() => onChange(pickWeekday(value, weekday))}
              className={cn(
                "h-7 rounded-md text-[11px] font-semibold uppercase tracking-wide transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45 disabled:cursor-default",
                on ? "text-primary" : "text-muted-foreground",
                weekdayPickable && "hover:bg-background",
              )}
            >
              {narrowDays[weekday]}
            </button>
          );
        })}
      </div>

      <div className="relative overflow-hidden">
        <AnimatePresence mode="popLayout" initial={false} custom={direction}>
          <motion.div
            key={month.toISOString()}
            custom={direction}
            initial={{ opacity: 0, x: direction * slide }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: direction * -slide }}
            transition={{ duration: reduceMotion ? 0 : 0.22, ease: [0.22, 1, 0.36, 1] }}
            className="grid grid-cols-7 gap-1"
            onMouseLeave={() => setHovered(null)}
          >
            {cells.map((day) => {
              const inMonth = day.getUTCMonth() === month.getUTCMonth();
              const past = day < today;
              const isToday = day.getTime() === today.getTime();
              const checks = !past && inMonth ? checkTimesOn(value, day, today) : [];
              const lit = checks.length > 0;
              const pickable = inMonth && !past;
              const cell = (
                <button
                  key={day.toISOString()}
                  type="button"
                  disabled={!pickable}
                  aria-label={dayLabel(day)}
                  aria-pressed={pickable && !hourly ? lit : undefined}
                  aria-haspopup={hourly ? "dialog" : undefined}
                  onClick={hourly ? undefined : () => onChange(pickDay(value, day, today))}
                  onMouseEnter={() => inMonth && setHovered(day)}
                  onFocus={() => inMonth && setHovered(day)}
                  className={cn(
                    "group grid h-10 place-items-center rounded-md text-sm tabular-nums",
                    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45 disabled:cursor-default",
                    !inMonth && "invisible",
                    past && "text-muted-foreground/45",
                  )}
                >
                  <span className="relative grid size-9 place-items-center">
                    <AnimatePresence>
                      {lit && (
                        <motion.span
                          aria-hidden
                          className="absolute inset-0 rounded-full bg-primary-fill shadow-sm"
                          initial={{ scale: reduceMotion ? 1 : 0.4, opacity: 0 }}
                          animate={{ scale: 1, opacity: 1 }}
                          exit={{ scale: reduceMotion ? 1 : 0.4, opacity: 0 }}
                          transition={{ type: "spring", stiffness: 520, damping: 30 }}
                        />
                      )}
                    </AnimatePresence>
                    {!lit && pickable && (
                      <span
                        aria-hidden
                        className="absolute inset-0 rounded-full opacity-0 ring-1 ring-primary/40 transition-opacity group-hover:opacity-100"
                      />
                    )}
                    <span
                      className={cn(
                        "relative transition-colors",
                        lit ? "font-semibold text-primary-foreground" : "",
                        isToday && !lit && "font-semibold text-primary",
                      )}
                    >
                      {day.getUTCDate()}
                    </span>
                    {isToday && (
                      <span
                        aria-hidden
                        className={cn(
                          "absolute bottom-0.5 size-1 rounded-full",
                          lit ? "bg-primary-foreground" : "bg-primary",
                        )}
                      />
                    )}
                    {checks.length > 1 && (
                      <span
                        aria-hidden
                        className="absolute -end-0.5 -top-0.5 grid min-w-4 place-items-center rounded-full bg-foreground px-1 text-[9px] font-semibold leading-4 text-background"
                      >
                        {checks.length}
                      </span>
                    )}
                  </span>
                </button>
              );
              if (!hourly || !pickable) return cell;
              const key = day.toISOString();
              return (
                <Popover
                  key={key}
                  open={clockDay === key}
                  onOpenChange={(open) => setClockDay(open ? key : null)}
                >
                  <PopoverTrigger asChild>{cell}</PopoverTrigger>
                  <PopoverContent className="w-auto p-4">
                    <HourClock
                      title={dayLabel(day)}
                      checks={checks}
                      minute={value.time.slice(3)}
                      onToggle={(time) => onChange(toggleSlot(value, day, time, today))}
                    />
                  </PopoverContent>
                </Popover>
              );
            })}
          </motion.div>
        </AnimatePresence>
      </div>

      <p className="mt-3 min-h-5 truncate text-xs text-muted-foreground" aria-live="polite">
        {readout ??
          msg(
            hourly
              ? "continuous_tab.schedule.calendar.hint.hourly"
              : value.frequency === "monthly"
                ? "continuous_tab.schedule.calendar.hint.monthly"
                : "continuous_tab.schedule.calendar.hint.weekly",
          )}
      </p>
    </div>
  );
}

const DIAL = 264;
const RING = 108;

/** A 24-hour clock face for one date: lit hours check, a click turns one hour on or off. */
function HourClock({
  title,
  checks,
  minute,
  onToggle,
}: {
  title: string;
  checks: string[];
  minute: string;
  onToggle: (time: string) => void;
}) {
  const locale = getActiveIntlLocale();
  const on = new Set(checks);
  return (
    <div className="space-y-3">
      <p className="text-center text-sm font-semibold">{title}</p>
      <div className="relative mx-auto rounded-full bg-muted/50" style={{ width: DIAL, height: DIAL }}>
        <div className="absolute inset-[34%] grid place-items-center rounded-full bg-background text-center shadow-xs">
          <span className="text-[11px] font-medium leading-tight text-muted-foreground">
            {msg("continuous_tab.schedule.calendar.checks", { count: checks.length })}
          </span>
        </div>
        {Array.from({ length: 24 }, (_, hour) => {
          const time = `${String(hour).padStart(2, "0")}:${minute}`;
          const lit = on.has(time);
          const angle = (hour / 24) * 2 * Math.PI - Math.PI / 2;
          return (
            <button
              key={hour}
              type="button"
              aria-pressed={lit}
              aria-label={formatClock(time, locale)}
              title={formatClock(time, locale)}
              onClick={() => onToggle(time)}
              style={{
                left: DIAL / 2 + RING * Math.cos(angle),
                top: DIAL / 2 + RING * Math.sin(angle),
              }}
              className={cn(
                "absolute grid size-6 -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full text-[11px] font-medium tabular-nums transition-[background-color,color,transform] duration-150 active:scale-90",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45",
                lit
                  ? "bg-primary-fill text-primary-foreground shadow-sm"
                  : "text-muted-foreground hover:bg-background hover:text-foreground",
              )}
            >
              {hour}
            </button>
          );
        })}
      </div>
      <p className="text-center text-xs text-muted-foreground">
        {msg("continuous_tab.schedule.calendar.clock_hint")}
      </p>
    </div>
  );
}
