"use client";

import { useEffect, useId, useRef, useSyncExternalStore } from "react";

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
import { SCENE_MARGIN, buildScene, type Scene } from "@/shared/ui/evolution-scene";

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

// A sine curve spreads the change over the whole morph. A cubic curve packs most
// of it into about 40 ms, and a change of posture then reads as a jump.
function easeInOutSine(t: number): number {
  return (1 - Math.cos(Math.PI * t)) / 2;
}

let evolution: Evolution | null = null;

/** Parsed and paired on first use, so pages that only import the splash pay nothing up front. */
function getEvolution(): Evolution {
  evolution ??= buildEvolution(ANIMALS);
  return evolution;
}

let scene: Scene | null = null;

/** The sea and the land that the animals cross; built only for a figure that shows them. */
function getScene(): Scene {
  scene ??= buildScene(getEvolution(), {
    firstHoldMs: FIRST_HOLD_MS,
    holdMs: HOLD_MS,
    morphMs: MORPH_MS,
  });
  return scene;
}

/**
 * The frame `elapsed` ms into the timeline; past the end it stays on the human.
 * With a scene the animals also swim and walk, at the pace of the real time `clockMs`.
 */
function frameAt(elapsed: number, world?: Scene, clockMs?: number): FramePart[] {
  const { animals, transitions } = getEvolution();
  let t = elapsed;
  for (let i = 0; i < transitions.length; i++) {
    const hold = i === 0 ? FIRST_HOLD_MS : HOLD_MS;
    if (t < hold) return restFrame(animals[i]!, world?.warp(i, elapsed, clockMs));
    t -= hold;
    if (t < MORPH_MS) {
      return morphFrame(
        transitions[i]!,
        easeInOutSine(t / MORPH_MS),
        world?.warp(i, elapsed, clockMs),
        world?.warp(i + 1, elapsed, clockMs),
      );
    }
    t -= MORPH_MS;
  }
  const last = animals.length - 1;
  return restFrame(animals[last]!, world?.warp(last, elapsed, clockMs));
}

const FILL: Record<NonNullable<PartFill>, string> = {
  ground: "var(--evolution-ground, var(--splash-ground))",
  ink: "currentColor",
};

const NON_SCALING = { vectorEffect: "non-scaling-stroke" } as const;

const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

function subscribeReducedMotion(onChange: () => void) {
  const query = window.matchMedia(REDUCED_MOTION);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

/**
 * Whether the user asked for reduced motion. The server cannot know, so server
 * markup hydrates as "no" and then corrects itself; a first render that differs
 * from the server markup is a hydration error.
 */
function useReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribeReducedMotion,
    () => window.matchMedia(REDUCED_MOTION).matches,
    () => false,
  );
}

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
 * `durationMs` replays the same timeline faster or slower so it ends at that
 * time: on the human, or with a scene, when the human stands.
 * `still` always shows the still human, for server-rendered markup that has to
 * honour reduced motion before hydration can read it.
 * `scene` gives the journey a place: the first fish swim in a sea, the sea bed
 * rises to a beach as the legs form, and the animals walk on over the land. The
 * world passes under the figure and each animal moves the way it travels. The
 * human runs the last stretch, then slows down and stands. The scene is wider
 * than the figure and fades out at its two sides.
 */
export function EvolutionMorph({
  width = 260,
  className,
  durationMs,
  still = false,
  scene: withScene = false,
}: {
  width?: number;
  className?: string;
  durationMs?: number;
  still?: boolean;
  scene?: boolean;
}) {
  const reduceMotion = useReducedMotion() || still;
  const pathRefs = useRef<Array<SVGPathElement | null>>([]);
  const worldRef = useRef<SVGGElement | null>(null);
  const waterRef = useRef<SVGGElement | null>(null);
  // The id goes into `url(#...)`, where the punctuation of a React id is not safe.
  const maskId = `evolution-scene-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const { animals, maxParts } = getEvolution();
  const world = withScene ? getScene() : undefined;
  const timelineMs = world?.durationMs ?? EVOLUTION_DURATION_MS;
  const playMs = durationMs ?? timelineMs;
  const initial = reduceMotion
    ? restFrame(animals[animals.length - 1]!)
    : restFrame(animals[0]!, world?.warp(0, 0));
  const camera = world?.camera(reduceMotion ? timelineMs : 0);

  useEffect(() => {
    if (reduceMotion) return;
    let frame = 0;
    let start: number | null = null;
    const tick = (now: number) => {
      start ??= now;
      const elapsed = ((now - start) * timelineMs) / playMs;
      const parts = frameAt(elapsed, world, now - start);
      pathRefs.current.forEach((el, i) => el && paint(el, parts[i]));
      if (world) {
        const at = world.camera(elapsed);
        const move = `translate(${at.x} ${at.y})`;
        worldRef.current?.setAttribute("transform", move);
        waterRef.current?.setAttribute("transform", move);
      }
      if (elapsed < timelineMs) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [reduceMotion, timelineMs, playMs, world]);

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
      {world && camera && (
        <>
          <defs>
            <linearGradient
              id={`${maskId}-sides`}
              gradientUnits="userSpaceOnUse"
              x1={-SCENE_MARGIN}
              x2={200 + SCENE_MARGIN}
              y1={0}
              y2={0}
            >
              <stop offset="0" stopColor="#fff" stopOpacity="0" />
              <stop offset="0.16" stopColor="#fff" />
              <stop offset="0.84" stopColor="#fff" />
              <stop offset="1" stopColor="#fff" stopOpacity="0" />
            </linearGradient>
            <linearGradient
              id={`${maskId}-floor`}
              gradientUnits="userSpaceOnUse"
              x1={0}
              x2={0}
              y1={world.groundY + 5}
              y2={world.groundY + 15}
            >
              <stop offset="0" stopColor="#000" stopOpacity="0" />
              <stop offset="1" stopColor="#000" />
            </linearGradient>
            {/* The world ends softly at its sides, and under the ground it stops short of what is below the figure. */}
            <mask
              id={maskId}
              maskUnits="userSpaceOnUse"
              x={-SCENE_MARGIN}
              y={0}
              width={200 + 2 * SCENE_MARGIN}
              height={world.groundY + 15}
            >
              <rect
                x={-SCENE_MARGIN}
                y={0}
                width={200 + 2 * SCENE_MARGIN}
                height={world.groundY + 15}
                fill={`url(#${maskId}-sides)`}
              />
              <rect
                x={-SCENE_MARGIN}
                y={0}
                width={200 + 2 * SCENE_MARGIN}
                height={world.groundY + 15}
                fill={`url(#${maskId}-floor)`}
              />
            </mask>
            {/* The sea left behind lies deeper than the floor of the scene. A soft floor
                turns its tint into a smudge, so the tint ends on a clean line. */}
            <clipPath id={`${maskId}-water`}>
              <rect
                x={-SCENE_MARGIN}
                y={0}
                width={200 + 2 * SCENE_MARGIN}
                height={world.groundY + 5}
              />
            </clipPath>
          </defs>
          <g mask={`url(#${maskId})`}>
            <g clipPath={`url(#${maskId}-water)`}>
              <g ref={waterRef} transform={`translate(${camera.x} ${camera.y})`}>
                <path d={world.art.water} fill="currentColor" fillOpacity={0.07} />
              </g>
            </g>
            <g
              ref={worldRef}
              transform={`translate(${camera.x} ${camera.y})`}
              stroke="currentColor"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d={world.art.surface} strokeWidth={1.2} opacity={0.4} {...NON_SCALING} />
              <path d={world.art.strata} strokeWidth={1.2} opacity={0.22} {...NON_SCALING} />
              <path d={world.art.plants} strokeWidth={1.4} opacity={0.42} {...NON_SCALING} />
              <path d={world.art.ground} strokeWidth={1.8} opacity={0.6} {...NON_SCALING} />
            </g>
          </g>
        </>
      )}
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
