/**
 * Morph engine for the ten-animal evolution figure (Haikouichthys → … → Homo sapiens).
 *
 * Each animal is drawn freely (see evolution-animals.ts), so consecutive animals do not
 * share a part list. For every step the engine first pairs parts that name the same body
 * part (body, head, each limb), so limbs map one to one, then pairs every other part with
 * its nearest counterpart of the same class (outline, stroke or ink dot) by position and size,
 * keeping each detail on the body part it decorates (a finger never becomes a toe),
 * resamples both at uniform arc length, and aligns their start point and direction so
 * the outline flows from one animal into the next. A new body part (a first fin, a far
 * leg, a head) grows out of the body through the whole step. Other parts with no
 * counterpart shrink and fade (leaving early) or grow and show (arriving late). One
 * outside the animal (a sail, a tail fin, a spear) shrinks into or grows out of the
 * animal, together with the parts that touch it; one inside it uses its own centre.
 * While an animal rests, its exact authored curves are drawn.
 *
 * A caller can pass a warp that moves the points of each part before they are drawn.
 * The scene uses it to make an animal swim or walk (see evolution-scene.ts).
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

export type Part = {
  kind: PartKind;
  role: PartRole | null;
  /** The named part this one sits on: its own role, or for a detail, the nearest named outline's. */
  host: PartRole | null;
  cls: PartClass;
  closed: boolean;
  fill: PartFill;
  /** Exact authored path, drawn while the animal rests. */
  d: string;
  /** The authored curve as a Bézier chain, for a rest that a warp moves. */
  bez: Float64Array;
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
    bez,
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

// How far outside a named part a detail can sit and still belong to it.
const HOST_REACH = 10;

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
    let nearest: Part | null = null;
    for (const o of named) {
      const d = nearestPoint(o.dense, p.cx, p.cy)[2];
      if (d < best) {
        best = d;
        nearest = o;
      }
    }
    // A detail far outside every named part (a sail ray, a spear tip) belongs to none of them.
    if (nearest && (best <= HOST_REACH || isInside(nearest.dense, p.cx, p.cy))) p.host = nearest.role;
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

/**
 * A point (x, y) on the outline of the named part `part`; `lead` marks the longest part of its
 * group. A `slow` group (a sail, a tail, a spear) is long, so it takes the whole morph to grow or shrink.
 */
type Anchor = { part: number; x: number; y: number; lead: boolean; slow: boolean };

/** A new body part of the next animal: it grows out of the point (ax, ay) of the body outline. */
type Bud = { b: number; ax: number; ay: number; to: Float64Array; closed: boolean; order: number };

/** What an unpaired part follows in a morph: the point at index `at` of the sampled outline `pair`, or a new body part. */
type Ride = { pair: Pair; at: number } | { bud: Bud };

export type Transition = {
  from: Animal;
  to: Animal;
  pairs: Pair[];
  /** The body of `from` that the buds grow out of, or -1. */
  body: number;
  buds: Bud[];
  leaving: number[];
  arriving: number[];
  /** Sampled shapes for unpaired parts, keyed `a<index>` (leaving) or `b<index>` (arriving). */
  lone: Map<string, Float64Array>;
  /** Where an unpaired part outside its animal shrinks to or grows from, with the keys of `lone`. */
  anchors: Map<string, Anchor>;
  /** The outline point that an unpaired part rides in this morph, with the keys of `lone`. */
  rides: Map<string, Ride>;
};

// How far apart (on the 100-unit animal scale) and how different in length two parts may be and still pair.
const PAIR_DISTANCE = 22;
const LENGTH_WEIGHT = 0.6;
// Outlines carry the silhouette, so they pair loosely (a shrew's stubby leg still becomes an
// ape's long one); detail strokes pair strictly, or else fade, rather than fly across the body.
const MAX_PAIR_COST: Record<PartClass, number> = { outline: 4, stroke: 2.2, dot: 2.2 };

// A stroke longer than this does not pair with one that points more than MAX_TURN away:
// the turn would sweep it across the figure, so the two fade instead.
const TURN_MIN_LENGTH = 10;
const MAX_TURN = Math.PI / 3;

/** The angle between the lines through the two ends of each open stroke (0 to 90 degrees). */
function turnBetween(a: Part, b: Part): number {
  const angle = (p: Part) => {
    const n = p.dense.length;
    return Math.atan2(p.dense[n - 1]! - p.dense[1]!, p.dense[n - 2]! - p.dense[0]!);
  };
  const d = Math.abs(angle(a) - angle(b)) % Math.PI;
  return Math.min(d, Math.PI - d);
}

function pairCost(a: Part, b: Part): number {
  // Named parts only ever become the same part of the next animal (see buildTransition).
  if (a.cls !== b.cls || a.role || b.role) return Infinity;
  if (a.host !== b.host) return Infinity;
  const apart = Math.hypot(a.nx - b.nx, a.ny - b.ny);
  // An outline with no name (a fin, a sail) pairs only with one at the same place on the animal.
  if (a.cls === "outline" && apart > PAIR_DISTANCE) return Infinity;
  // A mouth line that becomes an eye ring is a half ring on the way, and that reads as a smile.
  if (a.cls === "stroke" && a.closed !== b.closed) return Infinity;
  if (
    a.cls === "stroke" &&
    !a.closed &&
    !b.closed &&
    Math.min(a.length, b.length) > TURN_MIN_LENGTH &&
    turnBetween(a, b) > MAX_TURN
  ) {
    return Infinity;
  }
  return (
    apart / PAIR_DISTANCE +
    LENGTH_WEIGHT * Math.abs(Math.log((a.nlength + 1) / (b.nlength + 1)))
  );
}

/** The point of a polyline (x, y pairs) that is nearest to (x, y), and its distance. */
function nearestPoint(dense: Float64Array, x: number, y: number): [number, number, number] {
  let best: [number, number, number] = [x, y, Infinity];
  for (let i = 0; i + 3 < dense.length; i += 2) {
    const ax = dense[i]!;
    const ay = dense[i + 1]!;
    const dx = dense[i + 2]! - ax;
    const dy = dense[i + 3]! - ay;
    const k = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy || 1)));
    const d = Math.hypot(ax + dx * k - x, ay + dy * k - y);
    if (d < best[2]) best = [ax + dx * k, ay + dy * k, d];
  }
  return best;
}

/** Whether (x, y) is inside the closed outline (x, y pairs), by the even-odd rule. */
function isInside(dense: Float64Array, x: number, y: number): boolean {
  let inside = false;
  for (let i = 0, j = dense.length - 2; i < dense.length; j = i, i += 2) {
    const yi = dense[i + 1]!;
    const yj = dense[j + 1]!;
    if (yi > y !== yj > y && x < dense[i]! + ((y - yi) * (dense[j]! - dense[i]!)) / (yj - yi)) inside = !inside;
  }
  return inside;
}

// Unpaired parts this near to each other leave or arrive as one group.
const TOUCH = 3;
// A group with a lead longer than this grows or shrinks over the whole morph. In the short
// fade span a sail rises in about 40 ms, and that reads as a pop.
const SLOW_LENGTH = 15;

/** Whether two parts touch, or one has its centre inside the other. */
function touches(a: Part, b: Part, sa: Float64Array, sb: Float64Array): boolean {
  if ((b.closed && isInside(b.dense, a.cx, a.cy)) || (a.closed && isInside(a.dense, b.cx, b.cy))) return true;
  for (let i = 0; i < sa.length; i += 2) if (nearestPoint(b.dense, sa[i]!, sa[i + 1]!)[2] <= TOUCH) return true;
  for (let i = 0; i < sb.length; i += 2) if (nearestPoint(a.dense, sb[i]!, sb[i + 1]!)[2] <= TOUCH) return true;
  return false;
}

/**
 * The anchors of the unpaired parts `lone` of an animal. A part outside every named outline
 * gets the nearest point of a named outline. Parts that touch (a sail and its rays, a spear
 * and its tip) share the anchor of the longest one, so the group keeps its shape.
 */
function anchorsOf(animal: Animal, lone: number[], shape: (i: number) => Float64Array): Map<number, Anchor> {
  const parts = animal.parts;
  const named = parts.flatMap((p, i) => (p.role && p.cls === "outline" ? [i] : []));
  const outside = lone.filter((i) => !named.some((n) => isInside(parts[n]!.dense, parts[i]!.cx, parts[i]!.cy)));
  const leader = new Map(outside.map((i) => [i, i]));
  const find = (i: number): number => (leader.get(i) === i ? i : find(leader.get(i)!));
  outside.forEach((i, at) => {
    for (const j of outside.slice(at + 1)) {
      const [a, b] = [find(i), find(j)];
      if (a === b || !touches(parts[i]!, parts[j]!, shape(i), shape(j))) continue;
      // The longest part leads its group.
      if (parts[a]!.length >= parts[b]!.length) leader.set(b, a);
      else leader.set(a, b);
    }
  });
  const anchors = new Map<number, Anchor>();
  for (const i of outside) {
    const lead = parts[find(i)]!;
    let best: Anchor | null = null;
    let reach = Infinity;
    for (const n of named) {
      const [x, y, d] = nearestPoint(parts[n]!.dense, lead.cx, lead.cy);
      if (d < reach) {
        reach = d;
        best = { part: n, x, y, lead: find(i) === i, slow: lead.length > SLOW_LENGTH };
      }
    }
    if (best) anchors.set(i, best);
  }
  return anchors;
}

/**
 * The outline point that the unpaired part `i` of `animal` rides in the morph, so an eye stays
 * in its moving head and a sail stays on the moving back. `side` says which end of each pair
 * `animal` is.
 */
function rideOf(animal: Animal, side: "a" | "b", pairs: Pair[], buds: Bud[], i: number, anchor: Anchor | undefined): Ride | null {
  const p = animal.parts[i]!;
  // An unnamed outline has no host: it follows the front-most named outline that holds its centre (hair on a head).
  const holder = [...animal.parts].reverse().find((o) => o.role && o.cls === "outline" && isInside(o.dense, p.cx, p.cy));
  const role = anchor ? animal.parts[anchor.part]!.role : (p.host ?? holder?.role ?? null);
  const pair = role ? pairs.find((q) => animal.parts[q[side]]!.role === role) : undefined;
  if (!pair) {
    // A new head has no outline to ride: its eye and mouth grow out of the body with it.
    const bud = side === "b" && role ? buds.find((q) => animal.parts[q.b]!.role === role) : undefined;
    return bud ? { bud } : null;
  }
  const own = side === "a" ? pair.from : pair.to;
  const [x, y] = anchor ? [anchor.x, anchor.y] : [p.cx, p.cy];
  let at = 0;
  for (let k = 2; k < own.length; k += 2) {
    if (Math.hypot(own[k]! - x, own[k + 1]! - y) < Math.hypot(own[at]! - x, own[at + 1]! - y)) at = k;
  }
  return { pair, at };
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
  // A named outline with no counterpart starts at the point of the body that is nearest to it.
  const body = from.parts.findIndex((p) => p.role === "body");
  const toBody = to.parts.findIndex((p) => p.role === "body");
  const buds: Bud[] = [];
  to.parts.forEach((pb, b) => {
    if (body < 0 || usedB.has(b) || !pb.role || pb.cls !== "outline") return;
    usedB.add(b);
    const [ax, ay] = nearestPoint(from.parts[body]!.dense, pb.cx, pb.cy);
    // Until the parts take the order of the next animal, a far limb stays behind the body.
    const order = body + (b < toBody ? -0.5 : 0.5);
    buds.push({ b, ax, ay, to: resample(pb, SAMPLES.outline, pb.closed), closed: pb.closed, order });
  });
  const lone = new Map<string, Float64Array>();
  const leaving = from.parts.map((_, i) => i).filter((i) => !usedA.has(i));
  const arriving = to.parts.map((_, i) => i).filter((i) => !usedB.has(i));
  for (const i of leaving) lone.set(`a${i}`, resample(from.parts[i]!, SAMPLES[from.parts[i]!.cls], from.parts[i]!.closed));
  for (const i of arriving) lone.set(`b${i}`, resample(to.parts[i]!, SAMPLES[to.parts[i]!.cls], to.parts[i]!.closed));
  const anchors = new Map<string, Anchor>();
  for (const [i, anchor] of anchorsOf(from, leaving, (k) => lone.get(`a${k}`)!)) anchors.set(`a${i}`, anchor);
  for (const [i, anchor] of anchorsOf(to, arriving, (k) => lone.get(`b${k}`)!)) anchors.set(`b${i}`, anchor);
  const rides = new Map<string, Ride>();
  for (const i of leaving) {
    const ride = rideOf(from, "a", pairs, buds, i, anchors.get(`a${i}`));
    if (ride) rides.set(`a${i}`, ride);
  }
  for (const i of arriving) {
    const ride = rideOf(to, "b", pairs, buds, i, anchors.get(`b${i}`));
    if (ride) rides.set(`b${i}`, ride);
  }
  return { from, to, pairs, body, buds, leaving, arriving, lone, anchors, rides };
}

/** Moves the points of one part in place (x, y pairs). */
export type Warp = (part: Part, xy: Float64Array) => void;

/** The animal as authored, for the rests between morphs; a warp moves its curves. */
export function restFrame(animal: Animal, warp?: Warp): FramePart[] {
  return animal.parts.map((p) => {
    if (!warp) return { d: p.d, fill: p.fill, opacity: 1 };
    const bez = p.bez.slice();
    warp(p, bez);
    return { d: chainToPath(bez, p.closed), fill: p.fill, opacity: 1 };
  });
}

function warped(part: Part, xy: Float64Array, warp: Warp | undefined): Float64Array {
  if (!warp) return xy;
  const out = xy.slice();
  warp(part, out);
  return out;
}

// Leaving parts clear in the first 35% of a morph; arriving parts only show in the last 35%.
// The slow groups are the exception: they shrink into or grow out of the animal all the way.
const FADE_SPAN = 0.35;

/** The point an unpaired part scales about: its anchor on the animal, or its own centre. */
function pivot(animal: Animal, i: number, anchor: Anchor | undefined, warp: Warp | undefined): Float64Array {
  const p = animal.parts[i]!;
  if (!anchor) return warped(p, Float64Array.of(p.cx, p.cy), warp);
  return warped(animal.parts[anchor.part]!, Float64Array.of(anchor.x, anchor.y), warp);
}

/** The points scaled by `k` about (cx, cy), then moved by (dx, dy). */
function scaledAbout(p: Float64Array, cx: number, cy: number, k: number, dx: number, dy: number): Float64Array {
  const out = new Float64Array(p.length);
  for (let i = 0; i < p.length; i += 2) {
    out[i] = cx + (p[i]! - cx) * k + dx;
    out[i + 1] = cy + (p[i + 1]! - cy) * k + dy;
  }
  return out;
}

/**
 * The frame part of the unpaired part `i` of the `from` animal ("a") or the `to` animal ("b"),
 * at size and ink `k` (0 → 1), with the morph `t` of the way through. The parts that follow a
 * group leader (sail rays) only show above half size: smaller, their strokes run together
 * into a grey patch.
 */
function lonePart(tr: Transition, side: "a" | "b", i: number, k: number, t: number, warpFrom: Warp | undefined, warpTo: Warp | undefined): FramePart | null {
  const [animal, warp] = side === "a" ? [tr.from, warpFrom] : [tr.to, warpTo];
  const p = animal.parts[i]!;
  const anchor = tr.anchors.get(side + i);
  // The lead of a slow group changes size only, like a new body part. Other parts also fade.
  const opacity = anchor && !anchor.lead ? 2 * k - 1 : anchor?.slow ? 1 : k;
  if (opacity <= 0) return null;
  const c = pivot(animal, i, anchor, warp);
  let dx = 0;
  let dy = 0;
  const ride = tr.rides.get(side + i);
  if (ride && "bud" in ride) {
    const pts = scaledAbout(warped(p, tr.lone.get(side + i)!, warp), c[0]!, c[1]!, k, 0, 0);
    const root = warped(tr.from.parts[tr.body]!, Float64Array.of(ride.bud.ax, ride.bud.ay), warpFrom);
    return { d: pointsToPath(scaledAbout(pts, root[0]!, root[1]!, t, 0, 0), p.closed), fill: p.fill, opacity };
  }
  if (ride) {
    const { pair, at } = ride;
    const a = warped(tr.from.parts[pair.a]!, pair.from.slice(at, at + 2), warpFrom);
    const b = warped(tr.to.parts[pair.b]!, pair.to.slice(at, at + 2), warpTo);
    // The outline point is `t` of the way from a to b; the part sits at its own end of that path.
    const along = side === "a" ? t : t - 1;
    dx = (b[0]! - a[0]!) * along;
    dy = (b[1]! - a[1]!) * along;
  }
  const pts = scaledAbout(warped(p, tr.lone.get(side + i)!, warp), c[0]!, c[1]!, k, dx, dy);
  return { d: pointsToPath(pts, p.closed), fill: p.fill, opacity };
}

/**
 * The frame `t` (0 → 1, already eased) of the way through a transition, back to front.
 * Each animal keeps its own warp, so both ends move while one flows into the other.
 */
export function morphFrame(tr: Transition, t: number, warpFrom?: Warp, warpTo?: Warp): FramePart[] {
  const late = t >= 0.5;
  const keyed: Array<{ order: number; part: FramePart }> = [];
  for (const pair of tr.pairs) {
    const from = warped(tr.from.parts[pair.a]!, pair.from, warpFrom);
    const to = warped(tr.to.parts[pair.b]!, pair.to, warpTo);
    const pts = new Float64Array(from.length);
    for (let k = 0; k < pts.length; k++) pts[k] = from[k]! + (to[k]! - from[k]!) * t;
    const src = late ? tr.to.parts[pair.b]! : tr.from.parts[pair.a]!;
    keyed.push({
      order: late ? pair.b : pair.a,
      part: { d: pointsToPath(pts, pair.closed), fill: src.fill, opacity: 1 },
    });
  }
  for (const bud of tr.buds) {
    const p = tr.to.parts[bud.b]!;
    const anchor = warped(tr.from.parts[tr.body]!, Float64Array.of(bud.ax, bud.ay), warpFrom);
    const pts = warped(p, bud.to, warpTo).slice();
    for (let k = 0; k < pts.length; k++) pts[k] = anchor[k % 2]! + (pts[k]! - anchor[k % 2]!) * t;
    keyed.push({
      order: late ? bud.b : bud.order,
      part: { d: pointsToPath(pts, bud.closed), fill: p.fill, opacity: 1 },
    });
  }
  const out = Math.min(1, t / FADE_SPAN);
  for (const i of tr.leaving) {
    const k = tr.anchors.get(`a${i}`)?.slow ? 1 - t : 1 - out;
    const part = k > 0 ? lonePart(tr, "a", i, k, t, warpFrom, warpTo) : null;
    if (part) keyed.push({ order: late ? -1 : i, part });
  }
  const grow = Math.max(0, (t - (1 - FADE_SPAN)) / FADE_SPAN);
  for (const i of tr.arriving) {
    const k = tr.anchors.get(`b${i}`)?.slow ? t : grow;
    const part = k > 0 ? lonePart(tr, "b", i, k, t, warpFrom, warpTo) : null;
    // Only a slow group shows this early. It grows behind the animal that it grows out of.
    if (part) keyed.push({ order: late ? i : i - tr.to.parts.length, part });
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
    ...transitions.map((tr) => tr.pairs.length + tr.buds.length + tr.leaving.length + tr.arriving.length),
  );
  return { animals, transitions, maxParts };
}
