"use client";

import type { ReactNode } from "react";

/**
 * The banner shell every picker schematic is drawn into.
 *
 * The diagrams read input-to-output left-to-right in every locale, matching
 * the workflow canvas (which one of them depicts) rather than mirroring.
 */
export function BannerFrame({ children }: { children: ReactNode }) {
  return (
    // Sized only by container queries — mixing in a `sm:` height would leave
    // two utilities competing for `height` from different variant groups.
    <div className="relative h-36 shrink-0 border-b border-border/50 bg-muted/50 @xl:h-44 @3xl:h-auto @3xl:w-[45%] @3xl:border-b-0 @3xl:border-e">
      {/* The dot grid is CSS rather than an SVG pattern so it covers the whole
          banner — the schematic letterboxes inside the viewBox, a pattern fill
          would letterbox with it and leave bare bands at the sides. */}
      <div
        aria-hidden
        className="absolute inset-0"
        style={{
          backgroundImage: "radial-gradient(circle, color-mix(in srgb, var(--foreground) 7%, transparent) 1px, transparent 1px)",
          backgroundSize: "12px 12px",
        }}
      />
      <svg
        viewBox="0 0 240 88"
        preserveAspectRatio="xMidYMid meet"
        className="absolute inset-0 h-full w-full"
        aria-hidden="true"
      >
        {children}
      </svg>
    </div>
  );
}

export function GBox({
  x,
  y,
  w,
  h,
  accent = false,
}: {
  x: number;
  y: number;
  w: number;
  h: number;
  accent?: boolean;
}) {
  return (
    <rect
      x={x}
      y={y}
      width={w}
      height={h}
      rx={6}
      fill={accent ? "var(--primary)" : "var(--card)"}
      fillOpacity={accent ? 0.3 : 0.9}
      stroke="var(--foreground)"
      strokeOpacity={accent ? 0.4 : 0.22}
      strokeWidth={1.25}
    />
  );
}

export function GWire({ d, dashed = false }: { d: string; dashed?: boolean }) {
  return (
    <path
      d={d}
      fill="none"
      stroke="var(--foreground)"
      strokeOpacity={0.3}
      strokeWidth={1.25}
      strokeLinecap="round"
      // Dashed wires carry information (feedback, logs, migration) rather
      // than the candidate itself, so the two flows stay tellable apart.
      strokeDasharray={dashed ? "2.5 2.5" : undefined}
    />
  );
}

/** A bar standing in for a line of text or code inside a node; accented when it is the line that changed. */
export function GBar({ x, y, w, accent = false }: { x: number; y: number; w: number; accent?: boolean }) {
  return (
    <rect
      x={x}
      y={y}
      width={w}
      height={2.5}
      rx={1.25}
      fill={accent ? "var(--primary)" : "var(--foreground)"}
      fillOpacity={accent ? 0.85 : 0.3}
    />
  );
}

/** The arrowhead terminating a wire, tip at `(x, y)`. */
export function GArrow({ x, y, dir }: { x: number; y: number; dir: "up" | "down" | "left" | "right" }) {
  const points =
    dir === "up"
      ? `${x},${y} ${x - 2.5},${y + 4.5} ${x + 2.5},${y + 4.5}`
      : dir === "down"
        ? `${x},${y} ${x - 2.5},${y - 4.5} ${x + 2.5},${y - 4.5}`
        : dir === "left"
          ? `${x},${y} ${x + 4.5},${y - 2.5} ${x + 4.5},${y + 2.5}`
          : `${x},${y} ${x - 4.5},${y - 2.5} ${x - 4.5},${y + 2.5}`;
  return <polygon points={points} fill="var(--foreground)" fillOpacity={0.35} />;
}

/** One member of a population or one cell of a grid; `dim` marks a candidate that was dropped. */
export function GDot({
  cx,
  cy,
  r = 3.5,
  accent = false,
  dim = false,
}: {
  cx: number;
  cy: number;
  r?: number;
  accent?: boolean;
  dim?: boolean;
}) {
  return (
    <circle
      cx={cx}
      cy={cy}
      r={r}
      fill={accent ? "var(--primary)" : "var(--card)"}
      fillOpacity={accent ? 0.7 : dim ? 0.35 : 0.9}
      stroke="var(--foreground)"
      strokeOpacity={dim ? 0.1 : accent ? 0.45 : 0.28}
      strokeWidth={1.1}
    />
  );
}

/** A dashed boundary grouping nodes: an island, a harness, a front. */
export function GFrame({ x, y, w, h }: { x: number; y: number; w: number; h: number }) {
  return (
    <rect
      x={x}
      y={y}
      width={w}
      height={h}
      rx={8}
      fill="var(--foreground)"
      fillOpacity={0.03}
      stroke="var(--foreground)"
      strokeOpacity={0.25}
      strokeWidth={1}
      strokeDasharray="3 2.5"
    />
  );
}

/** A note with a folded corner: feedback, a hypothesis, a log. */
export function GNote({ x, y, w, h }: { x: number; y: number; w: number; h: number }) {
  const fold = 5;
  return (
    <g>
      <path
        d={`M${x} ${y} H${x + w - fold} L${x + w} ${y + fold} V${y + h} H${x} Z`}
        fill="var(--card)"
        fillOpacity={0.9}
        stroke="var(--foreground)"
        strokeOpacity={0.22}
        strokeWidth={1.25}
        strokeLinejoin="round"
      />
      <path
        d={`M${x + w - fold} ${y} V${y + fold} H${x + w}`}
        fill="none"
        stroke="var(--foreground)"
        strokeOpacity={0.22}
        strokeWidth={1}
        strokeLinejoin="round"
      />
    </g>
  );
}

/** A pass (check) or fail (cross) mark centred on `(x, y)`. */
export function GMark({ x, y, ok }: { x: number; y: number; ok: boolean }) {
  return (
    <path
      d={ok ? `M${x - 3} ${y} L${x - 1} ${y + 2.5} L${x + 3.5} ${y - 2.5}` : `M${x - 2.5} ${y - 2.5} L${x + 2.5} ${y + 2.5} M${x + 2.5} ${y - 2.5} L${x - 2.5} ${y + 2.5}`}
      fill="none"
      stroke={ok ? "var(--primary)" : "var(--foreground)"}
      strokeOpacity={ok ? 0.9 : 0.35}
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  );
}

/** A run: the play triangle centred on `(x, y)`. */
export function GPlay({ x, y }: { x: number; y: number }) {
  return <polygon points={`${x - 2.5},${y - 3.5} ${x + 3.5},${y} ${x - 2.5},${y + 3.5}`} fill="var(--foreground)" fillOpacity={0.4} />;
}

/** Scores as a small column chart standing on `y`; the last column is accented when it is the new best. */
export function GChart({
  x,
  y,
  heights,
  accentLast = false,
}: {
  x: number;
  y: number;
  heights: number[];
  accentLast?: boolean;
}) {
  return (
    <g>
      {heights.map((h, i) => (
        <rect
          key={i}
          x={x + i * 6}
          y={y - h}
          width={4}
          height={h}
          rx={1}
          fill={accentLast && i === heights.length - 1 ? "var(--primary)" : "var(--foreground)"}
          fillOpacity={accentLast && i === heights.length - 1 ? 0.8 : 0.28}
        />
      ))}
    </g>
  );
}
