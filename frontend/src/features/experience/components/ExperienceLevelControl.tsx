"use client";

import type { Icon } from "@phosphor-icons/react";

import { ArrowCounterClockwise, Compass, FadersHorizontal, Gauge, Wrench } from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/primitives/button";
import { Segmented } from "@/shared/ui/segmented";
import { SettingsRow } from "@/shared/ui/settings-row";

import { EXPERIENCE_LEVELS, type ExperienceLevel } from "../lib/abstraction";
import { useExperienceOptional } from "../providers/experience-provider";
import { LEVEL_KEYS } from "./level-keys";
import { TOUCH_TAP } from "./touch";

const LEVEL_ICONS: Record<ExperienceLevel, Icon> = {
  guided: Compass,
  standard: FadersHorizontal,
  expert: Wrench,
};

/**
 * The Guided / Standard / Expert picker that Settings shows.
 */
function ExperienceLevelSwitch() {
  const experience = useExperienceOptional();
  if (!experience) return null;
  const { level, loaded, setLevel } = experience;
  return (
    <Segmented
      value={level}
      onChange={(next) => void setLevel(next)}
      label={msg("experience.level.title")}
      segmentClassName="transition-[color,transform] duration-150 ease-out active:scale-[0.97] motion-reduce:transition-none motion-reduce:active:scale-100"
      options={EXPERIENCE_LEVELS.map((value) => {
        const LevelIcon = LEVEL_ICONS[value];
        return {
          value,
          label: msg(LEVEL_KEYS[value].label),
          tip: msg(LEVEL_KEYS[value].description),
          disabled: !loaded,
          icon: (
            <LevelIcon
              aria-hidden="true"
              weight={value === level ? "fill" : "bold"}
              className={cn(
                "size-3.5 shrink-0 transition-colors duration-200",
                value === level ? "text-primary" : "text-current",
              )}
            />
          ),
        };
      })}
    />
  );
}

/**
 * Settings' Guided / Standard / Expert picker and "Run the setup again".
 */
export function ExperienceLevelControl({ onRerun }: { onRerun?: () => void }) {
  const experience = useExperienceOptional();
  if (!experience) return null;
  const { loaded, rerunIntake } = experience;

  return (
    <>
      <div className="flex items-start gap-3 border-b border-border/40 py-3">
        <Gauge className="size-4 mt-0.5 text-muted-foreground shrink-0" aria-hidden="true" />
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="flex flex-col gap-0.5">
            <span className="text-sm font-medium text-foreground">
              {msg("experience.level.title")}
            </span>
            <span className="text-xs text-muted-foreground/80">
              {msg("experience.level.subtitle")}
            </span>
          </div>
          <ExperienceLevelSwitch />
        </div>
      </div>

      <SettingsRow
        icon={ArrowCounterClockwise}
        label={msg("experience.rerun.label")}
        description={msg("experience.rerun.description")}
      >
        <Button
          variant="outline"
          size="icon-sm"
          className={cn(
            TOUCH_TAP,
            "min-w-[44px] sm:min-w-0 [@media(hover:none)_and_(pointer:coarse)]:min-w-[44px]",
          )}
          disabled={!loaded}
          aria-label={msg("experience.rerun.action")}
          onClick={() => {
            onRerun?.();
            rerunIntake();
          }}
        >
          <ArrowCounterClockwise aria-hidden="true" />
        </Button>
      </SettingsRow>
    </>
  );
}
