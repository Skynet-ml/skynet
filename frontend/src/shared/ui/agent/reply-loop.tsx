"use client";

import * as React from "react";

import { cn } from "@/shared/lib/utils";

import { DOTS, dotAt } from "./reply-loop-path";

/** One loop: rest, silk, gather, slingshot, bloom and return. */
const LOOP_MS = 3500;
/** Each dot leaves a short fading trail of earlier positions. */
const TRAIL = [0.006, 0.012, 0.018, 0.024];
const DOT_R = 0.13;

/**
 * Three dots that move through one continuous loop while an agent replies.
 * The frame loop writes SVG attributes directly, so it never re-renders React.
 * Under reduced motion the dots rest in a row.
 */
export function ReplyLoop({ className }: { className?: string }) {
  const svgRef = React.useRef<SVGSVGElement>(null);

  React.useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const circles = Array.from(svg.querySelectorAll<SVGCircleElement>("circle[data-dot]"));
    const place = (t: number) => {
      for (const circle of circles) {
        const dot = Number(circle.dataset.dot);
        const [x, y] = dotAt(dot, t - Number(circle.dataset.lag));
        circle.setAttribute("cx", x.toFixed(3));
        circle.setAttribute("cy", (-y).toFixed(3));
      }
    };
    place(0);
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const start = performance.now();
    let frame = requestAnimationFrame(function tick(now) {
      place((now - start) / LOOP_MS);
      frame = requestAnimationFrame(tick);
    });
    return () => cancelAnimationFrame(frame);
  }, []);

  const dots = Array.from({ length: DOTS }, (_, dot) => dot);
  return (
    <svg ref={svgRef} viewBox="-1 -1 2 2" aria-hidden className={cn("overflow-visible", className)}>
      {TRAIL.map((lag, step) =>
        dots.map((dot) => (
          <circle
            key={`${dot}-${lag}`}
            data-dot={dot}
            data-lag={lag}
            r={DOT_R * (1 - (step + 1) * 0.16)}
            fill="currentColor"
            opacity={0.32 - step * 0.07}
          />
        )),
      )}
      {dots.map((dot) => (
        <circle key={dot} data-dot={dot} data-lag={0} r={DOT_R} fill="currentColor" />
      ))}
    </svg>
  );
}
