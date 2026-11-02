"use client";

import * as React from "react";

// BoardUI agent thinking "wave" (SKY-263): a 3x3 dot grid with a diagonal
// wavefront that glides from the top start corner to the bottom end corner.

const GRID = 3;
const SIZE = 4;
const GAP = 2;
const TICK_MS = 80;
const FADE_MS = 220;
const TRAIL = 0.3;
const MIN_OPACITY = 0.12;
const PHASE_STEP = 1 / 8;

// Static first frame: the same on server and client, and the resting state
// under reduced motion.
const SEED = [0.55, 0.3, 0.15, 0.85, 0.55, 0.3, 1, 0.85, 0.55];

function opacitiesAt(phase: number): number[] {
  const m = GRID - 1;
  return Array.from({ length: GRID * GRID }, (_, i) => {
    // Compress the scalar below 1 so the wrap reads as the front leaving the
    // grid and entering again.
    const s = (((i % GRID) + Math.floor(i / GRID)) / (2 * m)) * (GRID / (GRID + 1));
    const behind = (phase - s + 1) % 1;
    const lit = Math.max(0, 1 - behind / TRAIL) ** 1.5;
    return MIN_OPACITY + (1 - MIN_OPACITY) * lit;
  });
}

export function ThinkingDots({ className }: { className?: string }) {
  const [opacities, setOpacities] = React.useState<number[]>(SEED);

  React.useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let phase = 0;
    const id = window.setInterval(() => {
      phase = (phase + PHASE_STEP) % 1;
      setOpacities(opacitiesAt(phase));
    }, TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  return (
    <span
      aria-hidden
      className={className}
      style={{ display: "grid", gridTemplateColumns: `repeat(${GRID}, ${SIZE}px)`, gap: GAP }}
    >
      {opacities.map((opacity, i) => (
        <span
          key={i}
          className="rounded-[1px] bg-current"
          style={{ width: SIZE, height: SIZE, opacity, transition: `opacity ${FADE_MS}ms ease` }}
        />
      ))}
    </span>
  );
}
