"use client";

import * as React from "react";
import { effortFill, sliderStops, stepEffort } from "@/shared/lib/effort-slider";
import { effortLabel } from "@/shared/lib/model-efforts";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { ArrowCounterClockwise, Lightning, Sparkle } from "@/shared/ui/icons";
import { TooltipButton } from "@/shared/ui/tooltip-button";

interface EffortSliderProps {
  efforts: readonly string[];
  value: string | null;
  onChange: (effort: string | null) => void;
  /** The model the levels belong to, shown under the level name. */
  modelName: string;
  hintFor: (level: string | null) => string;
  /** The fast mode toggle; absent when the model does not take fast mode. */
  fast?: { on: boolean; onChange: (on: boolean) => void; note: string } | null;
}

/**
 * The effort picker for a local CLI model: a track from the model default to
 * the top level, with arrow keys, a reset to the default and, on models that
 * take it, a fast mode toggle. The fill and its glow grow with the level.
 */
export const EffortSlider = React.forwardRef<HTMLDivElement, EffortSliderProps>(
  function EffortSlider({ efforts, value, onChange, modelName, hintFor, fast }, ref) {
    const stops = sliderStops(efforts);
    const index = Math.max(0, stops.indexOf(value));
    const level = stops[index] ?? null;
    const fill = effortFill(efforts, level);
    const label = level ? effortLabel(level) : msg("agent.model_menu.effort_default");
    const hintId = React.useId();
    const trackRef = React.useRef<HTMLDivElement>(null);
    const dragging = React.useRef(false);

    const isRtl = (el: Element | null) => !!el && getComputedStyle(el).direction === "rtl";

    const pickAt = (clientX: number) => {
      const track = trackRef.current;
      if (!track || stops.length < 2) return;
      const rect = track.getBoundingClientRect();
      const along = isRtl(track) ? rect.right - clientX : clientX - rect.left;
      const at = Math.round((along / rect.width) * (stops.length - 1));
      const next = stops[Math.min(stops.length - 1, Math.max(0, at))] ?? null;
      if (next !== level) onChange(next);
    };

    const pct = `${fill * 100}%`;
    return (
      // Radix menus block Tab; letting it through reaches the reset and fast buttons.
      <div
        className="flex w-full flex-col gap-3 p-3"
        onKeyDown={(e) => {
          if (e.key === "Tab") e.stopPropagation();
        }}
      >
        <div className="flex items-start gap-2">
          {fast ? (
            <TooltipButton
              side="top"
              tooltip={
                <span className="flex flex-col gap-0.5">
                  <span>{msg("agent.model_menu.fast")}</span>
                  <span className="font-normal text-muted-foreground" dir="auto">
                    {fast.note}
                  </span>
                </span>
              }
            >
              <button
                type="button"
                aria-pressed={fast.on}
                aria-label={msg("agent.model_menu.fast")}
                onClick={() => fast.onChange(!fast.on)}
                className={cn(
                  "flex size-7 shrink-0 items-center justify-center rounded-md transition-colors",
                  "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
                  fast.on
                    ? "bg-primary/10 text-primary hover:bg-primary/15"
                    : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                )}
              >
                <Lightning aria-hidden className="size-3.5" />
              </button>
            </TooltipButton>
          ) : (
            // Balances the reset button so the level name stays centered.
            <span aria-hidden className="size-7 shrink-0" />
          )}
          <div className="flex min-w-0 flex-1 flex-col items-center">
            <span className="flex items-center gap-1.5 text-sm font-semibold">
              {/* The sparkle grows with the level and pops on each change. */}
              <Sparkle
                key={`spark-${level ?? "default"}`}
                aria-hidden
                className={cn(
                  "shrink-0 text-primary transition-[opacity,transform] duration-300 motion-reduce:transition-none",
                  "motion-safe:animate-in motion-safe:zoom-in-50 motion-safe:fade-in-0",
                )}
                style={{ opacity: 0.25 + 0.75 * fill, transform: `scale(${0.7 + 0.5 * fill})` }}
              />
              <span
                key={level ?? "default"}
                dir="auto"
                className={cn(
                  "motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-1 motion-safe:duration-200",
                  "text-primary",
                )}
              >
                {label}
              </span>
            </span>
            <span className="max-w-full truncate text-xs text-muted-foreground" dir="ltr">
              {modelName}
            </span>
          </div>
          <TooltipButton side="top" tooltip={msg("agent.model_menu.effort_reset")}>
            <button
              type="button"
              aria-label={msg("agent.model_menu.effort_reset")}
              disabled={level === null}
              onClick={() => onChange(null)}
              className={cn(
                "flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground",
                "hover:bg-accent/60 hover:text-foreground disabled:pointer-events-none disabled:opacity-40",
                "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
              )}
            >
              <ArrowCounterClockwise className="size-3.5 rtl:-scale-x-100" />
            </button>
          </TooltipButton>
        </div>

        <div
          ref={(el) => {
            trackRef.current = el;
            if (typeof ref === "function") ref(el);
            else if (ref) ref.current = el;
          }}
          role="slider"
          tabIndex={0}
          aria-label={msg("agent.model_menu.effort_label")}
          aria-valuemin={0}
          aria-valuemax={stops.length - 1}
          aria-valuenow={index}
          aria-valuetext={msg("agent.model_menu.effort_slider_value", {
            level: label,
            index: index + 1,
            count: stops.length,
          })}
          aria-describedby={hintId}
          onKeyDown={(e) => {
            const next = stepEffort(efforts, level, e.key, isRtl(e.currentTarget));
            if (next === undefined) return;
            // Radix reads arrows as menu moves (Left closes the submenu).
            e.preventDefault();
            e.stopPropagation();
            if (next !== level) onChange(next);
          }}
          onPointerDown={(e) => {
            dragging.current = true;
            e.currentTarget.setPointerCapture(e.pointerId);
            pickAt(e.clientX);
          }}
          onPointerMove={(e) => {
            if (dragging.current) pickAt(e.clientX);
          }}
          onPointerUp={() => {
            dragging.current = false;
          }}
          onPointerCancel={() => {
            dragging.current = false;
          }}
          className={cn(
            "relative h-6 cursor-pointer touch-none rounded-full",
            "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
          )}
        >
          <div className="absolute inset-x-0 top-1/2 h-2 -translate-y-1/2 overflow-hidden rounded-full bg-muted">
            <div
              className={cn(
                "absolute inset-y-0 start-0 rounded-full bg-primary transition-[width,box-shadow] duration-300 ease-out motion-reduce:transition-none",
              )}
              style={{
                width: pct,
                boxShadow: `0 0 ${4 + 10 * fill}px color-mix(in srgb, var(--primary) ${15 + 55 * fill}%, transparent)`,
              }}
            />
          </div>
          {stops.map((stop, i) => (
            <span
              key={stop ?? "default"}
              aria-hidden
              className={cn(
                "absolute top-1/2 size-1 -translate-x-1/2 -translate-y-1/2 rounded-full rtl:translate-x-1/2",
                i <= index ? "bg-primary-foreground/70" : "bg-muted-foreground/40",
              )}
              style={{ insetInlineStart: `${(i / Math.max(1, stops.length - 1)) * 100}%` }}
            />
          ))}
          <span
            aria-hidden
            className={cn(
              "absolute top-1/2 size-4 -translate-x-1/2 -translate-y-1/2 rounded-full rtl:translate-x-1/2",
              "border border-border bg-background shadow-sm",
              "transition-[inset-inline-start] duration-300 ease-out motion-reduce:transition-none",
            )}
            style={{ insetInlineStart: pct }}
          />
        </div>

        <div className="flex justify-between text-[11px] text-muted-foreground">
          <span>{msg("agent.model_menu.effort_faster")}</span>
          <span>{msg("agent.model_menu.effort_smarter")}</span>
        </div>
        <p id={hintId} className="text-xs text-muted-foreground" dir="auto">
          {hintFor(level)}
        </p>
      </div>
    );
  },
);
