"use client";

import { useEffect, useRef } from "react";
import { useReducedMotion } from "framer-motion";

import { ANIMALS } from "@/shared/ui/evolution-animals";
import {
  EVOLUTION_VIEWBOX,
  buildEvolution,
  morphFrame,
  restFrame,
  type Evolution,
  type FramePart,
  type PartFill,
} from "@/shared/ui/evolution-poses";

/**
 * Timing is fitted to the submit splash: the overlay holds for at least 2.2 s
 * (SUBMIT_SPLASH_HOLD_MS) and its content fades in from 0.3 s to 0.7 s. The first
 * fish is on screen as the content appears, then each animal rests for a beat and
 * flows into the next, like Factory's launch icons, landing on the human at 2.2 s.
 */
const FIRST_HOLD_MS = 378;
const HOLD_MS = 68;
const MORPH_MS = 142;

/** When the human lands; slow submits keep the overlay up and the human simply stays. */
const EVOLUTION_DURATION_MS =
  FIRST_HOLD_MS + (ANIMALS.length - 1) * MORPH_MS + (ANIMALS.length - 2) * HOLD_MS;

function easeInOutCubic(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
}

let evolution: Evolution | null = null;

/** Parsed and paired on first use, so pages that only import the splash pay nothing up front. */
function getEvolution(): Evolution {
  evolution ??= buildEvolution(ANIMALS);
  return evolution;
}

/** The frame `elapsed` ms into the timeline; past the end it stays on the human. */
function frameAt(elapsed: number): FramePart[] {
  const { animals, transitions } = getEvolution();
  let t = elapsed;
  for (let i = 0; i < transitions.length; i++) {
    const hold = i === 0 ? FIRST_HOLD_MS : HOLD_MS;
    if (t < hold) return restFrame(animals[i]!);
    t -= hold;
    if (t < MORPH_MS) return morphFrame(transitions[i]!, easeInOutCubic(t / MORPH_MS));
    t -= MORPH_MS;
  }
  return restFrame(animals[animals.length - 1]!);
}

const FILL: Record<NonNullable<PartFill>, string> = {
  ground: "var(--evolution-ground, var(--splash-ground))",
  ink: "currentColor",
};

function paint(el: SVGPathElement, part: FramePart | undefined) {
  if (!part) {
    el.setAttribute("d", "");
    return;
  }
  el.setAttribute("d", part.d);
  el.setAttribute("fill", part.fill ? FILL[part.fill] : "none");
  el.setAttribute("opacity", part.opacity === 1 ? "1" : part.opacity.toFixed(3));
}

/**
 * Ten animals on the line to us (Haikouichthys, Eusthenopteron, Tiktaalik,
 * Acanthostega, Dimetrodon, Thrinaxodon, Morganucodon, Proconsul, Lucy and a
 * spear-holding human), drawn as line figures that flow from one into the next
 * (the optimization submit splash).
 *
 * Ink follows `currentColor` (defaults to `--splash-ink`); hidden lines are
 * masked with `--evolution-ground`, which defaults to `--splash-ground`, so set
 * it when the figure sits on another surface. Reduced motion shows the human, still.
 * `durationMs` replays the same timeline faster or slower so it lands on the
 * human at that time (the app-load splash is shorter than the submit one).
 * `still` always shows the still human, for server-rendered markup that has to
 * honour reduced motion before hydration can read it.
 */
export function EvolutionMorph({
  width = 260,
  className,
  durationMs = EVOLUTION_DURATION_MS,
  still = false,
}: {
  width?: number;
  className?: string;
  durationMs?: number;
  still?: boolean;
}) {
  const reduceMotion = (useReducedMotion() ?? false) || still;
  const pathRefs = useRef<Array<SVGPathElement | null>>([]);
  const { animals, maxParts } = getEvolution();
  const initial = restFrame(reduceMotion ? animals[animals.length - 1]! : animals[0]!);

  useEffect(() => {
    if (reduceMotion) return;
    let frame = 0;
    let start: number | null = null;
    const tick = (now: number) => {
      start ??= now;
      const elapsed = ((now - start) * EVOLUTION_DURATION_MS) / durationMs;
      const parts = frameAt(elapsed);
      pathRefs.current.forEach((el, i) => el && paint(el, parts[i]));
      if (elapsed < EVOLUTION_DURATION_MS) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [reduceMotion, durationMs]);

  return (
    <svg
      viewBox={EVOLUTION_VIEWBOX}
      width={width}
      height={(width * 160) / 200}
      className={className}
      style={{ color: "var(--splash-ink)", overflow: "visible" }}
      fill="none"
      role="img"
      aria-label="A fish evolving step by step into a human"
    >
      {Array.from({ length: maxParts }, (_, i) => {
        const part = initial[i];
        return (
          <path
            key={i}
            ref={(el) => {
              pathRefs.current[i] = el;
            }}
            d={part?.d ?? ""}
            fill={part?.fill ? FILL[part.fill] : "none"}
            stroke="currentColor"
            strokeWidth={2.4}
            strokeLinecap="round"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
        );
      })}
    </svg>
  );
}
