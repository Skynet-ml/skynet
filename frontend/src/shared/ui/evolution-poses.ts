/**
 * Morph engine for the ten-animal evolution figure (Haikouichthys → … → Homo sapiens).
 *
 * Each animal is drawn freely (see evolution-animals.ts), so consecutive animals do not
 * share a part list. For every step the engine first pairs parts that name the same body
 * part (body, head, each limb), so limbs map one to one, then pairs every other part with
 * its nearest counterpart of the same class (outline, stroke or ink dot) by position and size,
 * keeping each detail on the body part it decorates (a finger never becomes a toe),
 * resamples both at uniform arc length, and aligns their start point and direction so
 * the outline flows from one animal into the next. Parts with no counterpart shrink
 * into their centre and fade (leaving early) or grow out of it (arriving late).
 * While an animal rests, its exact authored curves are drawn.
 */

import type { AnimalDef, PartKind, PartRole } from "./evolution-animals.ts";

export const EVOLUTION_VIEWBOX = "0 0 200 160";

export type PartFill = "ground" | "ink" | null;

/** One drawn path of a frame, in back-to-front order. */
export type FramePart = { d: string; fill: PartFill; opacity: number };

type Ctrl = { x: number; y: number; sharp: boolean };

/** Parse "x y x y* …" into control points. */
export function parsePoints(d: string): Ctrl[] {
  const tokens = d.trim().split(/[\s,]+/);
  const pts: Ctrl[] = [];
  for (let i = 0; i + 1 < tokens.length; i += 2) {
    const yToken = tokens[i + 1]!;
    const sharp = yToken.endsWith("*");
    pts.push({ x: Number(tokens[i]), y: Number(sharp ? yToken.slice(0, -1) : yToken), sharp });
  }
  return pts;
}

// Catmull-Rom tension: 1/6 of the neighbour chord gives the standard spline.
const TENSION = 1 / 6;

/** Turn control points into a smooth cubic Bézier chain [x0, y0, c1x, c1y, c2x, c2y, x1, y1, …]. */
function toBezier(pts: Ctrl[], closed: boolean): Float64Array {
  const n = pts.length;
  const spans = closed ? n : n - 1;
  const out = new Float64Array(2 + spans * 6);
  const at = (i: number) => pts[closed ? (i + n) % n : Math.max(0, Math.min(n - 1, i))]!;
  const tangent = (i: number) => {
    const p = at(i);
    if (p.sharp) return [0, 0];
    const a = at(i - 1);
    const b = at(i + 1);
    return [(b.x - a.x) * TENSION, (b.y - a.y) * TENSION];
  };
  out[0] = pts[0]!.x;
  out[1] = pts[0]!.y;
  for (let s = 0; s < spans; s++) {
    const p0 = at(s);
    const p1 = at(s + 1);
    const t0 = tangent(s);
    const t1 = tangent(s + 1);
    const k = 2 + s * 6;
    out[k] = p0.x + t0[0]!;
    out[k + 1] = p0.y + t0[1]!;
    out[k + 2] = p1.x - t1[0]!;
    out[k + 3] = p1.y - t1[1]!;
    out[k + 4] = p1.x;
    out[k + 5] = p1.y;
  }
  return out;
}

const r = (v: number) => Math.round(v * 10) / 10;

/** Serialise one Bézier chain as an SVG path. */
function chainToPath(c: Float64Array, closed: boolean): string {
  let d = `M${r(c[0]!)} ${r(c[1]!)}`;
  for (let k = 2; k < c.length; k += 6) {
    d += `C${r(c[k]!)} ${r(c[k + 1]!)} ${r(c[k + 2]!)} ${r(c[k + 3]!)} ${r(c[k + 4]!)} ${r(c[k + 5]!)}`;
  }
  return closed ? `${d}Z` : d;
}

/** Serialise sampled points as a polyline path. */
function pointsToPath(p: Float64Array, closed: boolean): string {
  let d = `M${r(p[0]!)} ${r(p[1]!)}`;
  for (let k = 2; k < p.length; k += 2) d += `L${r(p[k]!)} ${r(p[k + 1]!)}`;
  return closed ? `${d}Z` : d;
}

type PartClass = "outline" | "stroke" | "dot";

const CLASS_OF: Record<PartKind, PartClass> = {
  solid: "outline",
  limb: "outline",
  line: "stroke",
  ring: "stroke",
  dot: "dot",
};

const FILL_OF: Record<PartKind, PartFill> = { solid: "ground", limb: "ground", dot: "ink", line: null, ring: null };

// Samples per part while morphing: enough that a 100 ms flow reads as a smooth curve.
const SAMPLES: Record<PartClass, number> = { outline: 64, stroke: 16, dot: 12 };

type Part = {
  kind: PartKind;
  role: PartRole | null;
  /** The named part this one sits on: its own role, or for a detail, the nearest named outline's. */
  host: PartRole | null;
  cls: PartClass;
  closed: boolean;
  fill: PartFill;
  /** Exact authored path, drawn while the animal rests. */
  d: string;
  /** Dense polyline along the curve (x, y pairs) with cumulative length, for resampling. */
  dense: Float64Array;
  cum: Float64Array;
  cx: number;
  cy: number;
  length: number;
  /** Centre and length relative to the whole animal (its box scaled to 100), used for pairing. */
  nx: number;
  ny: number;
  nlength: number;
};

export type Animal = { key: string; name: string; parts: Part[] };

const DENSE_STEPS = 10;

function densify(bez: Float64Array): { dense: Float64Array; cum: Float64Array } {
  const spans = (bez.length - 2) / 6;
  const n = spans * DENSE_STEPS + 1;
  const dense = new Float64Array(n * 2);
  dense[0] = bez[0]!;
  dense[1] = bez[1]!;
  let w = 2;
  for (let s = 0; s < spans; s++) {
    const k = s * 6;
    const [x0, y0, x1, y1, x2, y2, x3, y3] = [
      bez[k]!, bez[k + 1]!, bez[k + 2]!, bez[k + 3]!, bez[k + 4]!, bez[k + 5]!, bez[k + 6]!, bez[k + 7]!,
    ];
    for (let j = 1; j <= DENSE_STEPS; j++) {
      const t = j / DENSE_STEPS;
      const u = 1 - t;
      dense[w++] = u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3;
      dense[w++] = u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3;
    }
  }

  const cum = new Float64Array(n);
  for (let i = 1; i < n; i++) {
    cum[i] = cum[i - 1]! + Math.hypot(dense[2 * i]! - dense[2 * i - 2]!, dense[2 * i + 1]! - dense[2 * i - 1]!);
  }
  return { dense, cum };
}

function buildPart(kind: PartKind, src: string, role: PartRole | null): Part {
  const closed = kind === "solid" || kind === "ring" || kind === "dot";
  const bez = toBezier(parsePoints(src), closed);
  const { dense, cum } = densify(bez);
  const n = cum.length;
  let cx = 0;
  let cy = 0;
  for (let i = 0; i < n; i++) {
    cx += dense[2 * i]!;
    cy += dense[2 * i + 1]!;
  }
  return {
    kind,
    role,
    host: role,
    cls: CLASS_OF[kind],
    closed,
    fill: FILL_OF[kind],
    d: chainToPath(bez, closed),
    dense,
    cum,
    cx: cx / n,
    cy: cy / n,
    length: cum[n - 1]!,
    nx: 0,
    ny: 0,
    nlength: 0,
  };
}

function buildAnimal({ key, name, parts }: AnimalDef): Animal {
  const built = parts.map(([kind, src, role]) => buildPart(kind, src, role ?? null));
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const p of built) {
    for (let i = 0; i < p.dense.length; i += 2) {
      x0 = Math.min(x0, p.dense[i]!);
      x1 = Math.max(x1, p.dense[i]!);
      y0 = Math.min(y0, p.dense[i + 1]!);
      y1 = Math.max(y1, p.dense[i + 1]!);
    }
  }
  // Pair parts by where they sit on the animal, not on the page: a shrew's body should
  // still find an ape's body even though the ape is drawn several times larger.
  const scale = 100 / Math.max(x1 - x0, y1 - y0, 1);
  for (const p of built) {
    p.nx = (p.cx - (x0 + x1) / 2) * scale;
    p.ny = (p.cy - (y0 + y1) / 2) * scale;
    p.nlength = p.length * scale;
  }
  const named = built.filter((p) => p.role);
  for (const p of built) {
    if (p.host || p.cls === "outline") continue;
    let best = Infinity;
    for (const o of named) {
      for (let i = 0; i < o.dense.length; i += 2) {
        const d = Math.hypot(o.dense[i]! - p.cx, o.dense[i + 1]! - p.cy);
        if (d < best) {
          best = d;
          p.host = o.role;
        }
      }
    }
  }
  return { key, name, parts: built };
}

/**
 * `count` points at uniform arc length. A loop leaves out its closing duplicate (the
 * path closes with Z); an open walk includes both ends, so a closed outline walked
 * open still draws its full loop.
 */
function resample(part: Part, count: number, loop: boolean): Float64Array {
  const { dense, cum, length } = part;
  const out = new Float64Array(count * 2);
  const steps = loop ? count : count - 1;
  let j = 0;
  for (let i = 0; i < count; i++) {
    const s = (length * i) / steps;
    while (j < cum.length - 2 && cum[j + 1]! < s) j++;
    const span = cum[j + 1]! - cum[j]! || 1;
    const t = Math.min(1, Math.max(0, (s - cum[j]!) / span));
    out[2 * i] = dense[2 * j]! + (dense[2 * j + 2]! - dense[2 * j]!) * t;
    out[2 * i + 1] = dense[2 * j + 1]! + (dense[2 * j + 3]! - dense[2 * j + 1]!) * t;
  }
  return out;
}

/** Reorder `b` (rotation for loops, direction for both) to sit closest to `a`, comparing shapes about their centres. */
function align(a: Float64Array, b: Float64Array, loop: boolean, ac: [number, number], bc: [number, number]): Float64Array {
  const n = a.length / 2;
  let best = Infinity;
  let bestShift = 0;
  let bestRev = false;
  for (const rev of [false, true]) {
    for (let shift = 0; shift < (loop ? n : 1); shift++) {
      let cost = 0;
      for (let i = 0; i < n && cost < best; i++) {
        let k = rev ? n - 1 - i : i;
        k = (k + shift) % n;
        const dx = a[2 * i]! - ac[0] - (b[2 * k]! - bc[0]);
        const dy = a[2 * i + 1]! - ac[1] - (b[2 * k + 1]! - bc[1]);
        cost += dx * dx + dy * dy;
      }
      if (cost < best) {
        best = cost;
        bestShift = shift;
        bestRev = rev;
      }
    }
  }
  const out = new Float64Array(b.length);
  for (let i = 0; i < n; i++) {
    const k = ((bestRev ? n - 1 - i : i) + bestShift) % n;
    out[2 * i] = b[2 * k]!;
    out[2 * i + 1] = b[2 * k + 1]!;
  }
  return out;
}

type Pair = { a: number; b: number; from: Float64Array; to: Float64Array; closed: boolean };

export type Transition = {
  from: Animal;
  to: Animal;
  pairs: Pair[];
  leaving: number[];
  arriving: number[];
  /** Sampled shapes for unpaired parts, which shrink into or grow out of their centre. */
  lone: Map<string, Float64Array>;
};

// How far apart (on the 100-unit animal scale) and how different in length two parts may be and still pair.
const PAIR_DISTANCE = 22;
const LENGTH_WEIGHT = 0.6;
// Outlines carry the silhouette, so they pair loosely (a shrew's stubby leg still becomes an
// ape's long one); detail strokes pair strictly, or else fade, rather than fly across the body.
const MAX_PAIR_COST: Record<PartClass, number> = { outline: 4, stroke: 2.2, dot: 2.2 };

function pairCost(a: Part, b: Part): number {
  // Named parts only ever become the same part of the next animal (see buildTransition).
  if (a.cls !== b.cls || a.role || b.role) return Infinity;
  if (a.host && b.host && a.host !== b.host) return Infinity;
  return (
    Math.hypot(a.nx - b.nx, a.ny - b.ny) / PAIR_DISTANCE +
    LENGTH_WEIGHT * Math.abs(Math.log((a.nlength + 1) / (b.nlength + 1)))
  );
}

function buildTransition(from: Animal, to: Animal): Transition {
  const candidates: Array<{ a: number; b: number; cost: number }> = [];
  from.parts.forEach((pa, a) => {
    const b = pa.role ? to.parts.findIndex((pb) => pb.role === pa.role) : -1;
    if (b >= 0) candidates.push({ a, b, cost: -1 });
  });
  from.parts.forEach((pa, a) =>
    to.parts.forEach((pb, b) => {
      const cost = pairCost(pa, pb);
      if (cost < MAX_PAIR_COST[pa.cls]) candidates.push({ a, b, cost });
    }),
  );
  candidates.sort((x, y) => x.cost - y.cost);
  const usedA = new Set<number>();
  const usedB = new Set<number>();
  const pairs: Pair[] = [];
  for (const { a, b } of candidates) {
    if (usedA.has(a) || usedB.has(b)) continue;
    usedA.add(a);
    usedB.add(b);
    const pa = from.parts[a]!;
    const pb = to.parts[b]!;
    const loop = pa.closed && pb.closed;
    const count = SAMPLES[pa.cls];
    const sa = resample(pa, count, loop);
    const sb = align(sa, resample(pb, count, loop), loop, [pa.cx, pa.cy], [pb.cx, pb.cy]);
    pairs.push({ a, b, from: sa, to: sb, closed: loop });
  }
  const lone = new Map<string, Float64Array>();
  const leaving = from.parts.map((_, i) => i).filter((i) => !usedA.has(i));
  const arriving = to.parts.map((_, i) => i).filter((i) => !usedB.has(i));
  for (const i of leaving) lone.set(`a${i}`, resample(from.parts[i]!, SAMPLES[from.parts[i]!.cls], from.parts[i]!.closed));
  for (const i of arriving) lone.set(`b${i}`, resample(to.parts[i]!, SAMPLES[to.parts[i]!.cls], to.parts[i]!.closed));
  return { from, to, pairs, leaving, arriving, lone };
}

/** The animal as authored, for the rests between morphs. */
export function restFrame(animal: Animal): FramePart[] {
  return animal.parts.map((p) => ({ d: p.d, fill: p.fill, opacity: 1 }));
}

// Leaving parts clear in the first 35% of a morph; arriving parts only show in the last 35%.
const FADE_SPAN = 0.35;

function scaledAbout(p: Float64Array, cx: number, cy: number, k: number): Float64Array {
  const out = new Float64Array(p.length);
  for (let i = 0; i < p.length; i += 2) {
    out[i] = cx + (p[i]! - cx) * k;
    out[i + 1] = cy + (p[i + 1]! - cy) * k;
  }
  return out;
}

/** The frame `t` (0 → 1, already eased) of the way through a transition, back to front. */
export function morphFrame(tr: Transition, t: number): FramePart[] {
  const late = t >= 0.5;
  const keyed: Array<{ order: number; part: FramePart }> = [];
  for (const pair of tr.pairs) {
    const pts = new Float64Array(pair.from.length);
    for (let k = 0; k < pts.length; k++) pts[k] = pair.from[k]! + (pair.to[k]! - pair.from[k]!) * t;
    const src = late ? tr.to.parts[pair.b]! : tr.from.parts[pair.a]!;
    keyed.push({
      order: late ? pair.b : pair.a,
      part: { d: pointsToPath(pts, pair.closed), fill: src.fill, opacity: 1 },
    });
  }
  const out = Math.min(1, t / FADE_SPAN);
  if (out < 1) {
    for (const i of tr.leaving) {
      const p = tr.from.parts[i]!;
      keyed.push({
        order: late ? -1 : i,
        part: { d: pointsToPath(scaledAbout(tr.lone.get(`a${i}`)!, p.cx, p.cy, 1 - out), p.closed), fill: p.fill, opacity: 1 - out },
      });
    }
  }
  const grow = Math.max(0, (t - (1 - FADE_SPAN)) / FADE_SPAN);
  if (grow > 0) {
    for (const i of tr.arriving) {
      const p = tr.to.parts[i]!;
      keyed.push({
        order: late ? i : Number.MAX_SAFE_INTEGER,
        part: { d: pointsToPath(scaledAbout(tr.lone.get(`b${i}`)!, p.cx, p.cy, grow), p.closed), fill: p.fill, opacity: grow },
      });
    }
  }
  return keyed.sort((x, y) => x.order - y.order).map((k) => k.part);
}

export type Evolution = {
  animals: Animal[];
  /** `transitions[i]` morphs `animals[i]` into `animals[i + 1]`. */
  transitions: Transition[];
  /** Most paths any frame draws, so the component can keep a fixed pool of elements. */
  maxParts: number;
};

/** Prepare the chain once: parse every animal and pair the parts of each consecutive two. */
export function buildEvolution(defs: AnimalDef[]): Evolution {
  const animals = defs.map(buildAnimal);
  const transitions = animals.slice(1).map((to, i) => buildTransition(animals[i]!, to));
  const maxParts = Math.max(
    ...animals.map((a) => a.parts.length),
    ...transitions.map((tr) => tr.pairs.length + tr.leaving.length + tr.arriving.length),
  );
  return { animals, transitions, maxParts };
}
