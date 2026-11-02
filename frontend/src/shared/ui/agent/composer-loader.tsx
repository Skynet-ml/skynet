"use client";

import * as React from "react";

import { cn } from "@/shared/lib/utils";

// BoardUI composer loader (SKY-263): a band of light that travels around the
// composer's rim while the agent works. Three strokes of the same dash, a
// wide bloom, a tight glow and a crisp line, each with its own blur. The
// gradient uses the orange of the current overlay (`--gradient-progress`),
// not the BoardUI teal. The light draws over the surface, so the surface's
// own border never hides the crisp line.

const COLORS = ["#8f4318", "#b4521f", "#e0935a", "#cf7a3a"] as const;
const SPEED_S = 4.5;
const INTENSITY = 0.7;
const BLOOM = 16;
const BLOOM_STRENGTH = 0.3;
const ARC_DEG = 120;
const LINE = 2.5;
const CRISP_BLUR = 0.5;

function mixHex(a: string, b: string): string {
  const channel = (hex: string, i: number) => parseInt(hex.slice(1 + i * 2, 3 + i * 2), 16);
  const parts = [0, 1, 2].map((i) => Math.round((channel(a, i) + channel(b, i)) / 2));
  return `rgb(${parts.join(",")})`;
}

const MID = mixHex(COLORS[1], COLORS[2]);

interface ComposerLoaderProps {
  children: React.ReactNode;
  /** Show the light. It fades in and out. */
  active: boolean;
  /** Corner radius of the wrapped surface, px. */
  radius: number;
  className?: string;
}

export function ComposerLoader({ children, active, radius, className }: ComposerLoaderProps) {
  const id = `cl-${React.useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const clipRef = React.useRef<HTMLSpanElement>(null);
  const [box, setBox] = React.useState({ w: 640, h: 52 });

  React.useEffect(() => {
    const el = clipRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() =>
      setBox({ w: Math.max(1, el.clientWidth), h: Math.max(1, el.clientHeight) }),
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const band = (ARC_DEG / 360) * 100;
  const corner = Math.min(radius, box.w / 2, box.h / 2);
  const perimeter = Math.max(1, 2 * (box.w + box.h) - 8 * corner + 2 * Math.PI * corner);
  // The softer layers start a few px behind the crisp head, so the head leads.
  const phaseOf = (len: number, backPx: number) => len - band + (backPx * 100) / perimeter;
  const blurId = (blur: number) => `${id}-b${String(blur).replace(".", "_")}`;

  const stroke = (width: number, blur: number, opacity: number, len: number, backPx: number) => (
    <rect
      x={0}
      y={0}
      width={box.w}
      height={box.h}
      rx={corner}
      pathLength={100}
      fill="none"
      stroke={`url(#${id})`}
      strokeWidth={width}
      strokeLinecap="round"
      strokeDasharray={`${len} ${100 - len}`}
      className="bui-composer-loader-rect"
      filter={`url(#${blurId(blur)})`}
      style={{
        opacity,
        animation: `bui-composer-loader-dash ${SPEED_S}s linear ${(phaseOf(len, backPx) * SPEED_S) / 100}s infinite`,
      }}
    />
  );

  return (
    <div className={cn("relative", className)}>
      {children}
      <span
        ref={clipRef}
        aria-hidden
        className="pointer-events-none absolute inset-0 overflow-hidden"
        style={{ borderRadius: radius, opacity: active ? 1 : 0, transition: "opacity 450ms ease" }}
      >
        {active && (
          <svg
            width="100%"
            height="100%"
            viewBox={`0 0 ${box.w} ${box.h}`}
            preserveAspectRatio="none"
            style={{ opacity: INTENSITY }}
          >
            <defs>
              <linearGradient id={id} x1="0" y1="0" x2="1" y2="0">
                <stop offset="0%" stopColor={COLORS[0]} />
                <stop offset="30%" stopColor={COLORS[1]} />
                <stop offset="50%" stopColor={MID} />
                <stop offset="70%" stopColor={COLORS[2]} />
                <stop offset="100%" stopColor={COLORS[3]} />
              </linearGradient>
              {[14, 6, CRISP_BLUR].map((blur) => (
                <filter key={blur} id={blurId(blur)} x="-50%" y="-50%" width="200%" height="200%">
                  <feGaussianBlur stdDeviation={blur} />
                </filter>
              ))}
            </defs>
            {stroke(BLOOM * 2, 14, BLOOM_STRENGTH, band * 0.9, BLOOM + 16)}
            {stroke(LINE * 3.2, 6, 0.8, band * 0.95, 10)}
            {stroke(LINE, CRISP_BLUR, 1, band, 0)}
          </svg>
        )}
      </span>
    </div>
  );
}
