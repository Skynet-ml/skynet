"use client";

import { motion, useReducedMotion } from "motion/react";

import { cn } from "@/shared/lib/utils";

// A token, so the dim deepens in dark mode and still reads as a dim there.
const DIM = { fill: "var(--spotlight-dim)" };

interface SpotlightMaskProps {
  targetRect: DOMRect | null;
  padding?: number;
  borderRadius?: number;
  isTransitioning?: boolean;
  /** Let clicks reach the framed part; the dim around it still blocks them. */
  interactive?: boolean;
}

export function SpotlightMask({
  targetRect,
  padding = 8,
  borderRadius = 12,
  isTransitioning = false,
  interactive = false,
}: SpotlightMaskProps) {
  const prefersReducedMotion = useReducedMotion();

  if (!targetRect) {
    return (
      <motion.svg
        aria-hidden="true"
        className="pointer-events-auto absolute inset-0 h-full w-full"
        initial={prefersReducedMotion ? false : { opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: prefersReducedMotion ? 0 : 0.18 }}
      >
        <rect x="0" y="0" width="100%" height="100%" style={DIM} />
      </motion.svg>
    );
  }

  const edge = 8;
  const x = Math.max(edge, targetRect.x - padding);
  const y = Math.max(edge, targetRect.y - padding);
  const right = Math.min(window.innerWidth - edge, targetRect.right + padding);
  const bottom = Math.min(window.innerHeight - edge, targetRect.bottom + padding);
  const w = Math.max(1, right - x);
  const h = Math.max(1, bottom - y);
  const transition = {
    duration: prefersReducedMotion ? 0 : 0.22,
    ease: [0.22, 1, 0.36, 1] as [number, number, number, number],
  };

  // A mask does not change hit testing, so the dim's share of the clicks is
  // taken by four plain blocks around the hole.
  const blockers = interactive
    ? [
        { left: 0, top: 0, right: 0, height: y },
        { left: 0, top: bottom, right: 0, bottom: 0 },
        { left: 0, top: y, width: x, height: h },
        { left: right, top: y, right: 0, height: h },
      ]
    : [];

  return (
    <>
      {blockers.map((style, i) => (
        <div key={i} aria-hidden="true" className="pointer-events-auto absolute" style={style} />
      ))}
      <motion.svg
        aria-hidden="true"
        className={cn(
          "absolute inset-0 h-full w-full",
          interactive ? "pointer-events-none" : "pointer-events-auto",
        )}
        initial={prefersReducedMotion ? false : { opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: prefersReducedMotion ? 0 : 0.18 }}
      >
        <defs>
          <mask id="setup-spotlight-mask">
            <rect x="0" y="0" width="100%" height="100%" fill="white" />
            <motion.rect
              animate={{ x, y, width: w, height: h, rx: borderRadius }}
              initial={false}
              transition={transition}
              fill="black"
            />
          </mask>
        </defs>

        <rect
          x="0"
          y="0"
          width="100%"
          height="100%"
          style={DIM}
          mask="url(#setup-spotlight-mask)"
        />

        <motion.rect
          animate={{
            x,
            y,
            width: w,
            height: h,
            rx: borderRadius,
            opacity: isTransitioning ? 0 : 1,
          }}
          initial={false}
          transition={transition}
          fill="none"
          stroke="var(--primary)"
          strokeOpacity={0.95}
          strokeWidth="2"
          vectorEffect="non-scaling-stroke"
        />

        <motion.rect
          animate={{
            x: x + 2,
            y: y + 2,
            width: Math.max(1, w - 4),
            height: Math.max(1, h - 4),
            rx: Math.max(0, borderRadius - 2),
            opacity: isTransitioning ? 0 : 1,
          }}
          initial={false}
          transition={transition}
          fill="none"
          stroke="rgba(255,255,255,0.72)"
          strokeWidth="1"
          vectorEffect="non-scaling-stroke"
        />

        <motion.rect
          x="0"
          y="0"
          width="100%"
          height="100%"
          style={DIM}
          initial={false}
          animate={{ opacity: isTransitioning ? 1 : 0 }}
          transition={{
            duration: prefersReducedMotion ? 0 : isTransitioning ? 0.14 : 0.2,
            ease: [0.22, 1, 0.36, 1],
          }}
        />
      </motion.svg>
    </>
  );
}
