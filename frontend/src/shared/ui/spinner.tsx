import type { CSSProperties } from "react";

import { cn } from "@/shared/lib/utils";

// BoardUI spinner (SKY-263): the same arc at three scales, a crisp line, a
// tight glow and a wide bloom, each on its own blur. The soft layers trail a
// few degrees, so the sharp head leads. Colour comes from currentColor.
// Reduced motion slows the orbit instead of stopping it: the motion is the
// message.

export type SpinnerSize = "xs" | "sm" | "md" | "lg" | "xl";

const SIZES: Record<SpinnerSize, { box: number; line: number }> = {
  xs: { box: 12, line: 1.5 },
  sm: { box: 16, line: 2 },
  md: { box: 20, line: 2 },
  lg: { box: 24, line: 2.5 },
  xl: { box: 32, line: 3 },
};

interface ArcProps {
  box: number;
  line: number;
  width: number;
  blur: number;
  opacity: number;
  arc: number;
  lag: number;
}

function Arc({ box, line, width, blur, opacity, arc, lag }: ArcProps) {
  const center = box / 2 - line / 2;
  const inner = center - width / 2;
  const outer = center + width / 2;
  // Grow the wrapper to hold the ring plus its blur, so nothing clips.
  const bleed = Math.max(0, outer + blur * 2 - box / 2);
  const ring = `radial-gradient(circle, transparent ${inner}px, #000 ${inner + 0.5}px, #000 ${outer - 0.5}px, transparent ${outer}px)`;
  const style: CSSProperties = {
    inset: -bleed,
    filter: blur > 0 ? `blur(${blur}px)` : undefined,
    opacity,
    rotate: `${-lag}deg`,
  };
  return (
    <span aria-hidden className="absolute" style={style}>
      <span
        className="absolute inset-0"
        style={{
          background: `conic-gradient(transparent ${360 - arc}deg, currentColor 360deg)`,
          maskImage: ring,
          WebkitMaskImage: ring,
        }}
      />
    </span>
  );
}

export interface SpinnerProps {
  size?: SpinnerSize;
  /** Colour comes from the text colour. Default `text-primary`. */
  className?: string;
}

/** Decorative spinner; the caller owns the accessible busy state. */
export function Spinner({ size = "md", className }: SpinnerProps) {
  const { box, line } = SIZES[size];
  return (
    <span
      aria-hidden
      className={cn("relative inline-flex shrink-0 text-primary", className)}
      style={{ width: box, height: box }}
    >
      <span
        className="absolute inset-0 rounded-full border-current opacity-15"
        style={{ borderWidth: line, borderStyle: "solid" }}
      />
      <span className="absolute inset-0 animate-spin [animation-duration:900ms] motion-reduce:[animation-duration:2400ms]">
        <Arc box={box} line={line} width={line * 4} blur={line * 1.6} opacity={0.32} arc={110} lag={22} />
        <Arc box={box} line={line} width={line * 2} blur={line * 0.6} opacity={0.6} arc={200} lag={9} />
        <Arc box={box} line={line} width={line} blur={0} opacity={1} arc={290} lag={0} />
        <span
          className="absolute left-1/2 top-0 rounded-full bg-current"
          style={{ width: line, height: line, translate: "-50% 0" }}
        />
      </span>
    </span>
  );
}
