"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { CircleNotch, PaperPlaneTilt } from "@/shared/ui/icons";
import { TooltipButton } from "@/shared/ui/tooltip-button";

// A thin pill track with a round knob that stands above it, as on the effort
// slider. The control stays 44px tall, so the knob is easy to grab by touch.
const KNOB_PX = 32;
const TRACK_PX = 16;
// A release past this point starts the run; a shorter drag springs back.
const LAUNCH_AT = 0.9;
// The same short spring as the effort slider's fill and thumb.
const SPRING =
  "duration-[320ms] ease-[cubic-bezier(0.34,1.3,0.64,1)] motion-reduce:transition-none";
// Angles (0deg points along the track to its end, negative is up) for the
// sparks that grow in number as the knob nears the end.
const SPARKS = [
  { a: -70, t: 1.3, delay: 0 },
  { a: 150, t: 1.7, delay: 0.45 },
  { a: -115, t: 1.1, delay: 0.8 },
  { a: 165, t: 1.9, delay: 0.2 },
  { a: -140, t: 1.5, delay: 1.05 },
  { a: -90, t: 2.1, delay: 0.6 },
];
// Gives a spark a new angle and length each cycle, as on the effort slider.
function rejumpSpark(e: React.AnimationEvent<HTMLSpanElement>) {
  const el = e.currentTarget;
  const angle = Math.random() < 0.75 ? -60 - Math.random() * 110 : 140 + Math.random() * 30;
  el.style.setProperty("--spark-a", `${Math.round(angle)}deg`);
  el.style.setProperty("--spark-r", (0.7 + Math.random() * 0.7).toFixed(2));
}
// Vertical position (% of the fill), period and delay for each streak of
// current along the fill once a release will launch.
const STREAKS = [
  { y: 22, t: 1.4, delay: 0 },
  { y: 58, t: 1.1, delay: 0.5 },
  { y: 38, t: 1.7, delay: 0.95 },
];

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

// A short buzz on phones that have it; desktop browsers ignore it.
function buzz(pattern: number | number[]) {
  if (typeof navigator !== "undefined" && "vibrate" in navigator) navigator.vibrate(pattern);
}

interface SlideToStartProps {
  /** Starts the run. */
  onStart: () => void;
  /** The run is starting; the knob stays at the end with a spinner. */
  starting: boolean;
  /** Why the run cannot start; the control is locked and shows it as a tooltip. */
  disabledReason: string | null;
}

/**
 * The control that starts an optimization: drag the knob to the end of the
 * track, with the effort slider's charge animation: a glow and sweep, two
 * sparks near the end, then six sparks, streaks and a pulse once a release
 * will launch. Arming and launch each play its flash and spray once. Enter
 * or Space on the knob starts it too, so the drag is never the only way.
 */
export function SlideToStart({ onStart, starting, disabledReason }: SlideToStartProps) {
  const trackRef = React.useRef<HTMLDivElement>(null);
  const drag = React.useRef<{ x: number; from: number; moved: boolean } | null>(null);
  const flight = React.useRef<number | undefined>(undefined);
  const hintId = React.useId();
  const [progress, setProgress] = React.useState(0);
  const [dragging, setDragging] = React.useState(false);
  const [launched, setLaunched] = React.useState(false);
  // A click flies the knob to the end first, so the launch plays the same.
  const [flying, setFlying] = React.useState(false);
  // Each count remounts the effort slider's flash and spray, so they play once.
  const [burst, setBurst] = React.useState(0);
  const locked = disabledReason !== null || starting || launched || flying;
  // A failed submit clears `starting`; the knob then returns to the start.
  const [wasStarting, setWasStarting] = React.useState(starting);
  if (wasStarting !== starting) {
    setWasStarting(starting);
    if (!starting) {
      setLaunched(false);
      setProgress(0);
    }
  }
  const at = starting || launched ? 1 : progress;
  const armed = at >= LAUNCH_AT;

  const fire = () => {
    setLaunched(true);
    setProgress(1);
    setBurst((b) => b + 1);
    buzz([14, 40, 24]);
    onStart();
  };
  const launch = () => {
    if (!locked) fire();
  };
  // Click: the knob springs to the end and arms, then launches.
  const flyAndLaunch = () => {
    setFlying(true);
    setProgress(1);
    setBurst((b) => b + 1);
    flight.current = window.setTimeout(() => {
      setFlying(false);
      fire();
    }, 380);
  };
  React.useEffect(() => () => window.clearTimeout(flight.current), []);

  const travel = () => {
    const track = trackRef.current;
    return track ? track.clientWidth - KNOB_PX : 0;
  };
  const rtl = () => !!trackRef.current && getComputedStyle(trackRef.current).direction === "rtl";

  const end = () => {
    const d = drag.current;
    drag.current = null;
    setDragging(false);
    if (!d) return;
    if (!d.moved) flyAndLaunch();
    else if (progress >= LAUNCH_AT) launch();
    else setProgress(0);
  };

  // The knob's center travels half a knob from each end of the control.
  const knobAt = `calc(${KNOB_PX / 2}px + (100% - ${KNOB_PX}px) * ${at})`;
  const live = dragging || launched || flying;
  // The effort slider's charge: six sparks at the top, two just below it.
  const sparks = !live ? 0 : armed ? SPARKS.length : at >= 0.7 ? 2 : 0;
  const pulse = sparks > 0;
  const spray = 2 + Math.round(at * 4);
  const sweep = { seconds: (5 - 3.6 * at).toFixed(2), strength: (0.18 + 0.2 * at).toFixed(2) };
  const ease = dragging ? "duration-0" : SPRING;

  return (
    <div
      ref={trackRef}
      className={cn(
        "effort-charge relative h-11 min-w-0 flex-1 self-center",
        disabledReason !== null && "opacity-60",
      )}
      // The heat follows the finger with no lag while dragging, then eases.
      style={
        {
          "--effort-heat": at,
          transition: dragging ? "none" : undefined,
          // .effort-charge paints an inset ring for the effort slider's rounded
          // card; on this plain box it shows as a rectangle near the end.
          boxShadow: "none",
        } as React.CSSProperties
      }
    >
      {/* The glow sits outside the track, which clips the fill to its pill. */}
      <span
        aria-hidden
        data-pulse={pulse || undefined}
        className={cn(
          "effort-glow pointer-events-none absolute top-1/2 -translate-y-1/2 rounded-full transition-[width]",
          ease,
        )}
        style={{
          height: TRACK_PX,
          insetInlineStart: KNOB_PX / 2 - TRACK_PX / 2,
          width: `calc(${TRACK_PX}px + (100% - ${KNOB_PX}px) * ${at})`,
        }}
      />
      <div
        className="absolute top-1/2 -translate-y-1/2 overflow-hidden rounded-full bg-muted shadow-[inset_0_1px_2px_rgb(0_0_0/0.08)] dark:bg-foreground/10"
        style={{ height: TRACK_PX, insetInline: KNOB_PX / 2 - TRACK_PX / 2 }}
      >
        <div
          aria-hidden
          className={cn(
            "effort-fill absolute inset-y-0 start-0 overflow-hidden rounded-full shadow-[inset_0_1px_0_rgb(255_255_255/0.16)] transition-[width]",
            ease,
          )}
          // In the track's own box: the fill ends half a track past the knob's center.
          style={{ width: `calc(${TRACK_PX}px + (100% - ${TRACK_PX}px) * ${at})` }}
        >
          {pulse &&
            armed &&
            STREAKS.map((s) => (
              <span
                key={s.y}
                aria-hidden
                className="effort-streak"
                style={
                  {
                    top: `calc(${s.y}% - 3px)`,
                    "--streak-t": `${s.t}s`,
                    "--streak-delay": `${s.delay}s`,
                  } as React.CSSProperties
                }
              >
                <Streak />
              </span>
            ))}
          {live && at > 0 && (
            // The effort slider's sweep: faster and brighter as the charge builds.
            <span
              aria-hidden
              className="effort-sweep"
              style={
                {
                  "--sweep-seconds": `${sweep.seconds}s`,
                  "--sweep-strength": sweep.strength,
                } as React.CSSProperties
              }
            />
          )}
        </div>
      </div>
      {/* The finish: a dot at the end that lights up once a release will launch. */}
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute top-1/2 size-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full rtl:translate-x-1/2",
          "transition-[scale,background-color] duration-150 ease-out motion-reduce:transition-none",
          armed ? "scale-[1.8] bg-primary-foreground" : "bg-muted-foreground/45",
        )}
        style={{ insetInlineStart: `calc(100% - ${KNOB_PX / 2}px)` }}
      />
      <span
        aria-hidden
        data-pulse={pulse || undefined}
        className={cn(
          "effort-thumb-glow pointer-events-none absolute top-1/2 size-14 -translate-x-1/2 -translate-y-1/2 rounded-full rtl:translate-x-1/2",
          "transition-[inset-inline-start]",
          ease,
        )}
        style={{ insetInlineStart: knobAt }}
      />
      <TooltipButton
        side="top"
        tooltip={disabledReason ?? msg("submit.nav.slide_tooltip")}
        // One line, so the box hugs the text: a wrapped, balanced line keeps the
        // full 240px width and leaves empty space at the sides.
        contentClassName="max-w-none whitespace-nowrap px-2 py-1 text-center"
      >
        <button
          type="button"
          aria-label={
            starting
              ? msg("auto.features.submit.components.submitnav.3")
              : msg("submit.nav.slide_to_start")
          }
          aria-describedby={hintId}
          aria-disabled={locked || undefined}
          aria-busy={starting || undefined}
          data-tutorial="submit-button"
          data-telemetry="submit-run"
          onKeyDown={(e) => {
            if (e.key !== "Enter" && e.key !== " ") return;
            e.preventDefault();
            launch();
          }}
          onPointerDown={(e) => {
            if (locked || e.button !== 0) return;
            e.currentTarget.setPointerCapture(e.pointerId);
            drag.current = { x: e.clientX, from: progress, moved: false };
            setDragging(true);
          }}
          onPointerMove={(e) => {
            const d = drag.current;
            const span = travel();
            if (!d || span <= 0) return;
            const dx = (e.clientX - d.x) * (rtl() ? -1 : 1);
            // A few pixels of jitter still count as a click.
            if (Math.abs(dx) > 3) d.moved = true;
            if (!d.moved) return;
            const next = Math.min(1, Math.max(0, d.from + dx / span));
            // One tick when the knob crosses into the launch zone.
            if (next >= LAUNCH_AT && progress < LAUNCH_AT) {
              buzz(8);
              setBurst((b) => b + 1);
            }
            setProgress(next);
          }}
          onPointerUp={end}
          onPointerCancel={end}
          className={cn(
            "group absolute top-1/2 flex -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full rtl:translate-x-1/2",
            // The setup dialog's current-step dot: an open primary ring with a halo.
            "bg-background text-primary ring-2 ring-primary ring-offset-2 ring-offset-background touch-none",
            "transition-[inset-inline-start,scale]",
            ease,
            locked
              ? "cursor-not-allowed"
              : "cursor-grab hover:scale-[1.06] active:cursor-grabbing active:scale-[1.12]",
            armed && dragging && "scale-[1.15]",
            "focus-visible:outline-2 focus-visible:outline-offset-[6px] focus-visible:outline-ring/60",
          )}
          style={{ insetInlineStart: knobAt, width: KNOB_PX, height: KNOB_PX }}
        >
          {!locked && !dragging && (
            <span
              aria-hidden
              className="pointer-events-none absolute -inset-2 rounded-full bg-primary/15 motion-safe:animate-pulse"
            />
          )}
          {/* The submit page's paper plane: it flaps while you hover or drag,
            takes off at launch, and a spinner takes its place. */}
          <span
            aria-hidden
            data-flap={(dragging && !launched) || undefined}
            data-armed={(dragging && armed) || undefined}
            className={cn(
              "plane-flap relative block",
              launched && "plane-takeoff motion-reduce:invisible",
              starting && !launched && "invisible",
            )}
          >
            <PaperPlaneTilt className="size-[18px] rtl:-scale-x-100" weight="fill" />
          </span>
          {starting && (
            <CircleNotch
              aria-hidden
              className="absolute size-4 animate-spin motion-safe:animate-in motion-safe:fade-in-0 motion-safe:delay-300 motion-safe:fill-mode-backwards motion-reduce:animate-none"
            />
          )}
        </button>
      </TooltipButton>
      <p id={hintId} className="sr-only">
        {msg("submit.nav.slide_hint")}
      </p>
      {/* A zero-size anchor on the knob's center; mirrored in RTL so the
          bolts fly toward the end there too. */}
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute top-1/2 size-0 rtl:-scale-x-100",
          "transition-[inset-inline-start]",
          ease,
        )}
        style={{ insetInlineStart: knobAt }}
      >
        {SPARKS.slice(0, sparks).map((s) => (
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
        {burst > 0 && (
          <span key={burst}>
            <span
              className="effort-flash"
              style={{ "--flash-o": 0.45 + at * 0.55 } as React.CSSProperties}
            />
            {Array.from({ length: spray }, (_, i) => (
              <span
                key={i}
                className="effort-spray"
                style={
                  {
                    "--spark-a": `${-160 + (110 * (i + 0.5)) / spray}deg`,
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
  );
}
