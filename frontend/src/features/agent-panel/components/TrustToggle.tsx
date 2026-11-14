"use client";

import * as React from "react";
import { FastForward, Pause, Play } from "@/shared/ui/icons";
import { formatMsg } from "@/shared/lib/messages";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { cn } from "@/shared/lib/utils";

import { TRUST_MODE_DESCRIPTION, TRUST_MODE_HUE, TRUST_MODE_LABEL } from "../hooks/use-trust-mode";
import type { TrustMode } from "../lib/types";

interface TrustToggleProps {
  mode: TrustMode;
  onCycle: () => void;
  className?: string;
  /** Borderless variant for the composer's control row, as tall as the
   *  composer's other buttons. It shows the icon and the mode name. */
  plain?: boolean;
}

// A media scale from strict to free: pause asks first, play runs the safe
// steps, fast-forward runs everything at full speed.
const ICONS: Record<TrustMode, React.ComponentType<{ className?: string }>> = {
  ask: Pause,
  auto_safe: Play,
  yolo: FastForward,
};

const MODE_ORDER: TrustMode[] = ["ask", "auto_safe", "yolo"];

export function TrustToggle({ mode, onCycle, className, plain }: TrustToggleProps) {
  const Icon = ICONS[mode];
  const hue = TRUST_MODE_HUE[mode];
  const label = TRUST_MODE_LABEL[mode]();
  // A focused button's new name is not read reliably, so a tap also writes
  // the new mode to a live region. It stays empty until the first tap.
  const [announce, setAnnounce] = React.useState(false);

  return (
    <>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={() => {
              setAnnounce(true);
              onCycle();
            }}
            aria-label={formatMsg("auto.features.agent.panel.components.trusttoggle.template.1", {
              p1: label,
            })}
            className={cn(
              plain
                ? "inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full px-2.5 text-xs"
                : "inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[0.6875rem]",
              "min-h-[44px] transition-all duration-150 hover:bg-accent/60 active:scale-[0.97] cursor-pointer md:min-h-0 [@media(hover:none)_and_(pointer:coarse)]:min-h-[44px]",
              className,
            )}
            style={
              plain
                ? { color: hue }
                : {
                    borderColor: `color-mix(in srgb, ${hue} 19%, transparent)`,
                    color: hue,
                    backgroundColor: `color-mix(in srgb, ${hue} 4%, transparent)`,
                  }
            }
          >
            <Icon className={plain ? "size-4" : "size-3"} aria-hidden="true" />
            <span className="font-medium leading-none">{label}</span>
          </button>
        </TooltipTrigger>
        <TooltipContent side="top" className="w-max max-w-[min(20rem,var(--radix-tooltip-content-available-width))] px-2 py-1.5 text-start text-wrap font-normal">
          <ul className="space-y-1">
            {MODE_ORDER.map((m) => {
              const ModeIcon = ICONS[m];
              const active = m === mode;
              return (
                <li
                  key={m}
                  className={cn(
                    "flex items-start gap-1.5 leading-tight",
                    active ? "opacity-100" : "opacity-60",
                  )}
                >
                  <ModeIcon className="size-3 shrink-0 mt-[2px]" aria-hidden="true" />
                  <span className={active ? "font-semibold" : undefined}>
                    {TRUST_MODE_DESCRIPTION[m]()}
                  </span>
                </li>
              );
            })}
          </ul>
        </TooltipContent>
      </Tooltip>
      <span aria-live="polite" className="sr-only">
        {announce ? label : ""}
      </span>
    </>
  );
}
