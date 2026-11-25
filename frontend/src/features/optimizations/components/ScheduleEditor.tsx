"use client";

import { useMemo, useState } from "react";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { NumberInput } from "@/shared/ui/number-input";
import { Segmented } from "@/shared/ui/segmented";
import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import type { ScheduleFrequency, ScheduleRule } from "@/shared/types/continuous";
import { cn } from "@/shared/lib/utils";
import {
  FREQUENCIES,
  MAX_EVERY,
  PRESETS,
  applyPreset,
  presetOf,
  scheduleProblem,
  scheduleSummary,
  timeZoneOptions,
  type SchedulePreset,
} from "../lib/continuous-schedule";
import { ScheduleCalendar } from "./ScheduleCalendar";

const PRESET_KEYS: Record<SchedulePreset, MessageKey> = {
  hourly: "continuous_tab.schedule.preset.hourly",
  daily: "continuous_tab.schedule.preset.daily",
  weekdays: "continuous_tab.schedule.preset.weekdays",
  weekly: "continuous_tab.schedule.preset.weekly",
  monthly: "continuous_tab.schedule.preset.monthly",
  custom: "continuous_tab.schedule.preset.custom",
};

const UNIT_KEYS: Record<ScheduleFrequency, MessageKey> = {
  hourly: "continuous_tab.schedule.unit.hourly",
  daily: "continuous_tab.schedule.unit.daily",
  weekly: "continuous_tab.schedule.unit.weekly",
  monthly: "continuous_tab.schedule.unit.monthly",
};

/** The rule in words, with its time zone. */
export function describeSchedule(rule: ScheduleRule): string {
  const { key, params } = scheduleSummary(rule, getActiveIntlLocale());
  return formatMsg("continuous_tab.schedule.in_zone", {
    schedule: formatMsg(key, params),
    timezone: rule.timezone.replaceAll("_", " "),
  });
}

/**
 * When a continuous plan checks, edited in one calendar card: the schedule
 * in words, one-click presets, a month calendar where the check days are
 * picked by clicking dates, and the time and time zone along its foot.
 */
export function ScheduleEditor({
  value,
  onChange,
  idPrefix,
}: {
  value: ScheduleRule;
  onChange: (rule: ScheduleRule) => void;
  idPrefix: string;
}) {
  const locale = getActiveIntlLocale();
  const preset = presetOf(value, locale);
  const problem = scheduleProblem(value);
  const zones = useMemo(() => timeZoneOptions(value.timezone), [value.timezone]);
  const [customPicked, setCustomPicked] = useState(false);

  const set = (patch: Partial<ScheduleRule>) => onChange({ ...value, ...patch });
  const pickPreset = (next: SchedulePreset) => {
    setCustomPicked(next === "custom");
    if (next !== "custom") onChange(applyPreset(value, next, locale));
  };
  const hourly = value.frequency === "hourly";
  // "Custom" keeps the rule as it is, so the choice is held here until the rule itself differs.
  const showCustom = customPicked || preset === "custom";
  const field = "space-y-1.5";

  return (
    <div className="w-full max-w-xl overflow-hidden rounded-2xl border border-border/60 bg-card shadow-sm">
      <div className="space-y-4 p-4 sm:p-5">
        <p className="text-base font-semibold leading-snug" aria-live="polite">
          {problem ? (
            <span className="text-[var(--warning)]">{msg(problem)}</span>
          ) : (
            describeSchedule(value)
          )}
        </p>
        {/* Six segments cut every name on a phone, so narrow screens get a dropdown. */}
        <Segmented
          value={showCustom ? "custom" : preset}
          onChange={pickPreset}
          label={msg("continuous_tab.schedule.label")}
          size="sm"
          className="hidden w-full sm:grid"
          options={PRESETS.map((p) => ({ value: p, label: msg(PRESET_KEYS[p]) }))}
        />
        <Select
          value={showCustom ? "custom" : preset}
          onValueChange={(next) => pickPreset(next as SchedulePreset)}
        >
          <SelectTrigger
            className="w-full sm:hidden"
            aria-label={msg("continuous_tab.schedule.label")}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PRESETS.map((p) => (
              <SelectItem key={p} value={p}>
                {msg(PRESET_KEYS[p])}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <ScheduleCalendar value={value} onChange={onChange} />
      </div>

      <div className="flex flex-wrap items-end gap-x-3 gap-y-3 border-t border-border/60 bg-muted/40 px-4 py-3 sm:px-5">
        {showCustom && (
          <div className={field}>
            <Label htmlFor={`${idPrefix}-every`} className="text-xs text-muted-foreground">
              {msg("continuous_tab.schedule.every")}
            </Label>
            <div className="flex gap-2">
              <NumberInput
                id={`${idPrefix}-every`}
                value={value.every}
                min={1}
                max={MAX_EVERY[value.frequency]}
                onChange={(every) => set({ every })}
                className="w-24"
              />
              <Select
                value={value.frequency}
                onValueChange={(frequency) => {
                  const next = frequency as ScheduleFrequency;
                  set({
                    frequency: next,
                    every: Math.min(value.every, MAX_EVERY[next]),
                    weekdays:
                      next === "weekly" && value.weekdays.length === 0 ? [0] : value.weekdays,
                  });
                }}
              >
                <SelectTrigger className="w-28" aria-label={msg("continuous_tab.schedule.unit")}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {FREQUENCIES.map((f) => (
                    <SelectItem key={f} value={f}>
                      {msg(UNIT_KEYS[f])}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        )}

        <div className={field}>
          <Label htmlFor={`${idPrefix}-time`} className="text-xs text-muted-foreground">
            {msg(hourly ? "continuous_tab.schedule.start_at" : "continuous_tab.schedule.at")}
          </Label>
          <Input
            id={`${idPrefix}-time`}
            type="time"
            dir="ltr"
            required
            value={value.time}
            onChange={(e) => set({ time: e.target.value })}
            className="w-32 bg-background"
          />
        </div>

        <div className={cn(field, "min-w-0 flex-1")}>
          <Label htmlFor={`${idPrefix}-zone`} className="text-xs text-muted-foreground">
            {msg("continuous_tab.schedule.timezone")}
          </Label>
          <Select value={value.timezone} onValueChange={(timezone) => set({ timezone })}>
            <SelectTrigger id={`${idPrefix}-zone`} className="w-full min-w-48 bg-background">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {zones.map((zone) => (
                <SelectItem key={zone} value={zone}>
                  <bdi>{zone.replaceAll("_", " ")}</bdi>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {value.frequency === "monthly" && value.month_day > 28 && (
          <p className="basis-full text-xs text-muted-foreground">
            {msg("continuous_tab.schedule.month_end")}
          </p>
        )}
      </div>
    </div>
  );
}
