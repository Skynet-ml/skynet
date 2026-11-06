"use client";

import * as React from "react";
import {
  effortCharge,
  effortFill,
  fillMotion,
  sliderStops,
  stepEffort,
} from "@/shared/lib/effort-slider";
import { effortLabel } from "@/shared/lib/model-efforts";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { ArrowCounterClockwise, CaretRight, Lightning } from "@/shared/ui/icons";
import { TooltipButton } from "@/shared/ui/tooltip-button";

// Track height in px; the thumb is a little taller than the track.
const TRACK_PX = 22;
// A short spring: the fill and thumb overshoot a hair and settle.
const SPRING =
  "duration-[320ms] ease-[cubic-bezier(0.34,1.3,0.64,1)] motion-reduce:transition-none";

// Angles (0deg points along the track toward the top level, negative is up),
// periods and delays for the idle sparks; uneven so they never fall in step.
// No angle points within 60deg of straight ahead or steeply down: the top
// level puts the thumb near the panel edge, and a bolt must stay inside it.
const SPARKS = [
  { a: -70, t: 1.3, delay: 0 },
  { a: 150, t: 1.7, delay: 0.45 },
  { a: -115, t: 1.1, delay: 0.8 },
  { a: 165, t: 1.9, delay: 0.2 },
  { a: -140, t: 1.5, delay: 1.05 },
  { a: -90, t: 2.1, delay: 0.6 },
];

// Gives a spark a new angle and length each cycle, so the bolts seem to jump.
// Upward bolts take -60 to -170deg; a few go back and down, at 140 to 170deg.
function rejumpSpark(e: React.AnimationEvent<HTMLSpanElement>) {
  const el = e.currentTarget;
  const angle = Math.random() < 0.75 ? -60 - Math.random() * 110 : 140 + Math.random() * 30;
  el.style.setProperty("--spark-a", `${Math.round(angle)}deg`);
  el.style.setProperty("--spark-r", (0.7 + Math.random() * 0.7).toFixed(2));
}

// Vertical position (% of the fill), period and delay for each streak of
// current along the fill at the top level.
const STREAKS = [
  { y: 22, t: 1.4, delay: 0 },
  { y: 58, t: 1.1, delay: 0.5 },
  { y: 38, t: 1.7, delay: 0.95 },
];

function Streak() {
  return (
    <svg viewBox="0 0 60 8" preserveAspectRatio="none" className="block size-full" aria-hidden>
      <path
        d="M0 4 14 3 22 5.5 34 2.5 44 4.6 60 3.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="0.9"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

function Bolt() {
  return (
    <svg viewBox="0 0 12 7" className="block size-full overflow-visible" aria-hidden>
      <path
        d="M0 3.5 3.5 1.2 5.6 4.6 8.4 1.6 12 3.4"
        fill="none"
        stroke="var(--effort-volt)"
        strokeWidth="2.6"
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity="0.55"
      />
      <path
        d="M0 3.5 3.5 1.2 5.6 4.6 8.4 1.6 12 3.4"
        fill="none"
        stroke="var(--effort-arc)"
        strokeWidth="1.1"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

const REDUCED = "(prefers-reduced-motion: reduce)";

function useReducedMotion(): boolean {
  return React.useSyncExternalStore(
    (notify) => {
      if (typeof window.matchMedia !== "function") return () => {};
      const query = window.matchMedia(REDUCED);
      query.addEventListener("change", notify);
      return () => query.removeEventListener("change", notify);
    },
    () => typeof window.matchMedia === "function" && window.matchMedia(REDUCED).matches,
    () => false,
  );
}

/** The screen-reader hint for a slider level; `null` is the model default. */
export function effortHint(level: string | null): string {
  switch (level) {
    case "none":
      return msg("agent.model_menu.effort_none_hint");
    case "minimal":
      return msg("agent.model_menu.effort_minimal_hint");
    case "low":
      return msg("agent.model_menu.effort_low_hint");
    case "medium":
      return msg("agent.model_menu.effort_medium_hint");
    case "high":
      return msg("agent.model_menu.effort_high_hint");
    case "xhigh":
      return msg("agent.model_menu.effort_xhigh_hint");
    case "max":
      return msg("agent.model_menu.effort_max_hint");
    default:
      return msg("agent.model_menu.effort_default_hint");
  }
}

interface EffortSliderProps {
  efforts: readonly string[];
  value: string | null;
  onChange: (effort: string | null) => void;
  /** The model the levels belong to, shown under the level name; absent shows no model line. */
  modelName?: string;
  /** Opens the model list; the model name becomes its button. */
  onPickModel?: () => void;
  /** The reset-to-default button; the first stop and Home also reach the default. */
  showReset?: boolean;
  hintFor: (level: string | null) => string;
  /** The fast mode toggle; absent when the model does not take fast mode. */
  fast?: { on: boolean; onChange: (on: boolean) => void; note: string } | null;
}

/**
 * The effort picker for a local CLI model: a track from the model default to
 * the top level, with arrow keys, a reset to the default and, on models that
 * take it, a fast mode toggle. The model name opens the model list.
 */
export const EffortSlider = React.forwardRef<HTMLDivElement, EffortSliderProps>(
  function EffortSlider(
    { efforts, value, onChange, modelName, onPickModel, showReset = true, hintFor, fast },
    ref,
  ) {
    const stops = sliderStops(efforts);
    const index = Math.max(0, stops.indexOf(value));
    const level = stops[index] ?? null;
    const fill = effortFill(efforts, level);
    const motion = fillMotion(fill);
    const charge = effortCharge(efforts, level, useReducedMotion());
    // Each step up remounts the burst, so its flash and spray play once.
    const [seen, setSeen] = React.useState({ index, burst: 0 });
    if (seen.index !== index) {
      setSeen({ index, burst: index > seen.index ? seen.burst + 1 : seen.burst });
    }
    const label = level ? effortLabel(level) : msg("agent.model_menu.effort_default");
    const hintId = React.useId();
    const trackRef = React.useRef<HTMLDivElement>(null);
    const dragging = React.useRef(false);

    const isRtl = (el: Element | null) => !!el && getComputedStyle(el).direction === "rtl";

    const pickAt = (clientX: number) => {
      const track = trackRef.current;
      if (!track || stops.length < 2) return;
      const rect = track.getBoundingClientRect();
      const along = (isRtl(track) ? rect.right - clientX : clientX - rect.left) - TRACK_PX / 2;
      const at = Math.round((along / (rect.width - TRACK_PX)) * (stops.length - 1));
      const next = stops[Math.min(stops.length - 1, Math.max(0, at))] ?? null;
      if (next !== level) onChange(next);
    };

    // The thumb's center travels inside the track, half a track height from
    // each end, so the end stops sit inside the pill.
    const inset = TRACK_PX / 2;
    const at = (t: number) => `calc(${inset}px + (100% - ${TRACK_PX}px) * ${t})`;
    const iconButton = cn(
      "flex size-9 shrink-0 items-center justify-center rounded-full transition-colors",
      "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
    );
    return (
      // Radix menus block Tab; letting it through reaches the reset and fast buttons.
      <div
        className="effort-charge flex w-full flex-col gap-4 px-4 pb-5 pt-4"
        style={{ "--effort-heat": charge.heat } as React.CSSProperties}
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
                  iconButton,
                  fast.on
                    ? "text-primary hover:bg-primary/10"
                    : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                )}
              >
                <Lightning aria-hidden weight={fast.on ? "fill" : "bold"} className="size-5" />
              </button>
            </TooltipButton>
          ) : showReset ? (
            // Balances the reset button so the level name stays centered.
            <span aria-hidden className="size-9 shrink-0" />
          ) : null}
          <div className="flex min-w-0 flex-1 flex-col items-center gap-0.5 pt-1">
            <span
              key={level ?? "default"}
              dir="auto"
              className={cn(
                "effort-level text-xl font-semibold leading-7 tracking-[-0.01em]",
                "motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-1 motion-safe:duration-200",
              )}
            >
              {label}
            </span>
            {onPickModel ? (
              <button
                type="button"
                aria-label={`${msg("agent.model_menu.label")}: ${modelName}`}
                onClick={onPickModel}
                className={cn(
                  "flex max-w-full items-center gap-1 rounded-md px-1.5 py-0.5 text-sm text-muted-foreground transition-colors",
                  "hover:text-foreground",
                  "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
                )}
              >
                <span className="truncate" dir="ltr">
                  {modelName}
                </span>
                <CaretRight aria-hidden className="size-3.5 shrink-0 rtl:rotate-180" />
              </button>
            ) : modelName ? (
              <span
                className="max-w-full truncate px-1.5 py-0.5 text-sm text-muted-foreground"
                dir="ltr"
              >
                {modelName}
              </span>
            ) : null}
          </div>
          {showReset ? (
            <TooltipButton side="top" tooltip={msg("agent.model_menu.effort_reset")}>
              <button
                type="button"
                aria-label={msg("agent.model_menu.effort_reset")}
                disabled={level === null}
                onClick={() => onChange(null)}
                className={cn(
                  iconButton,
                  "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                  "disabled:pointer-events-none disabled:opacity-40",
                )}
              >
                <ArrowCounterClockwise className="size-5 rtl:-scale-x-100" />
              </button>
            </TooltipButton>
          ) : fast ? (
            <span aria-hidden className="size-9 shrink-0" />
          ) : null}
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
            "group relative h-7 cursor-pointer touch-none rounded-full",
            "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
          )}
        >
          {/* The glow sits outside the track, which clips the fill to its pill. */}
          <span
            aria-hidden
            data-pulse={charge.pulse || undefined}
            className={cn(
              "effort-glow pointer-events-none absolute start-0 top-1/2 h-[22px] -translate-y-1/2 rounded-full transition-[width]",
              SPRING,
            )}
            style={{ width: `calc(${TRACK_PX}px + (100% - ${TRACK_PX}px) * ${fill})` }}
          />
          <div className="absolute inset-x-0 top-1/2 h-[22px] -translate-y-1/2 overflow-hidden rounded-full bg-muted shadow-[inset_0_1px_2px_rgb(0_0_0/0.08)] dark:bg-foreground/10">
            <div
              className={cn(
                "effort-fill absolute inset-y-0 start-0 overflow-hidden rounded-full shadow-[inset_0_1px_0_rgb(255_255_255/0.16)] transition-[width]",
                SPRING,
              )}
              style={{ width: `calc(${TRACK_PX}px + (100% - ${TRACK_PX}px) * ${fill})` }}
            >
              {charge.pulse &&
                charge.heat === 1 &&
                STREAKS.map((s) => (
                  <span
                    key={s.y}
                    aria-hidden
                    className="effort-streak"
                    style={
                      {
                        top: `calc(${s.y}% - 4px)`,
                        "--streak-t": `${s.t}s`,
                        "--streak-delay": `${s.delay}s`,
                      } as React.CSSProperties
                    }
                  >
                    <Streak />
                  </span>
                ))}
              {motion && charge.heat > 0 && (
                // Keyed by level so a new level starts its sweep from the start.
                <span
                  key={level ?? "default"}
                  aria-hidden
                  className="effort-sweep"
                  style={
                    {
                      "--sweep-seconds": `${motion.seconds}s`,
                      "--sweep-strength": motion.strength,
                    } as React.CSSProperties
                  }
                />
              )}
            </div>
          </div>
          {stops.map((stop, i) => (
            <span
              key={stop ?? "default"}
              aria-hidden
              className={cn(
                "absolute top-1/2 size-[5px] -translate-x-1/2 -translate-y-1/2 rounded-full rtl:translate-x-1/2",
                i <= index ? "bg-primary-foreground/60" : "bg-muted-foreground/45",
              )}
              style={{ insetInlineStart: at(i / Math.max(1, stops.length - 1)) }}
            />
          ))}
          <span
            aria-hidden
            data-pulse={charge.pulse || undefined}
            className={cn(
              "effort-thumb-glow pointer-events-none absolute top-1/2 size-12 -translate-x-1/2 -translate-y-1/2 rounded-full rtl:translate-x-1/2",
              "transition-[inset-inline-start]",
              SPRING,
            )}
            style={{ insetInlineStart: at(fill) }}
          />
          <span
            aria-hidden
            className={cn(
              "effort-thumb absolute top-1/2 size-7 -translate-x-1/2 -translate-y-1/2 rounded-full rtl:translate-x-1/2",
              "bg-white ring-1 ring-black/[0.06]",
              "group-hover:scale-[1.04] group-active:scale-[1.1]",
              "transition-[inset-inline-start,scale]",
              SPRING,
            )}
            style={{ insetInlineStart: at(fill) }}
          />
          {/* A zero-size anchor on the thumb's center; mirrored in RTL so the
              bolts fly toward the top level there too. */}
          <span
            aria-hidden
            className={cn(
              "pointer-events-none absolute top-1/2 size-0 rtl:-scale-x-100",
              "transition-[inset-inline-start]",
              SPRING,
            )}
            style={{ insetInlineStart: at(fill) }}
          >
            {SPARKS.slice(0, charge.sparks).map((s) => (
              <span
                key={s.a}
                className="effort-spark"
                onAnimationIteration={rejumpSpark}
                style={
                  {
                    "--spark-a": `${s.a}deg`,
                    "--spark-t": `${s.t}s`,
                    "--spark-delay": `${s.delay}s`,
                  } as React.CSSProperties
                }
              >
                <Bolt />
              </span>
            ))}
            {seen.burst > 0 && charge.spray > 0 && (
              <span key={seen.burst}>
                <span
                  className="effort-flash"
                  style={{ "--flash-o": 0.45 + charge.heat * 0.55 } as React.CSSProperties}
                />
                {Array.from({ length: charge.spray }, (_, i) => (
                  <span
                    key={i}
                    className="effort-spray"
                    style={
                      {
                        "--spark-a": `${-160 + (110 * (i + 0.5)) / charge.spray}deg`,
                        "--spark-delay": `${i * 18}ms`,
                      } as React.CSSProperties
                    }
                  >
                    <Bolt />
                  </span>
                ))}
              </span>
            )}
          </span>
        </div>
        <p id={hintId} className="sr-only">
          {hintFor(level)}
        </p>
      </div>
    );
  },
);
