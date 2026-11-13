/**
 * Where each of the three reply-loop dots sits at a point of the loop.
 *
 * One continuous loop in six phases: rest (three dots in a row), silk (they
 * unfold along a figure eight), gather (the weave pulls into a chase),
 * slingshot (the chase speeds around the rim), bloom (it opens into a turning
 * triangle) and return (the triangle folds back into the row). Each phase is a
 * function of the whole loop, and neighbouring phases cross-fade, so the dots
 * never jump. The loop ends where it starts.
 *
 * Positions are in a unit box centred on 0 with y pointing up; the rim is at
 * radius {@link RIM}.
 */

export const DOTS = 3;
export const RIM = 0.62;

const ROW_GAP = 0.32;
const TAU = Math.PI * 2;

const REST_END = 0.12;
const SILK_END = 0.5;
const SLING_END = 0.72;
const BLOOM_END = 0.9;

/** The silk weave ends a quarter turn into its figure eight: up and right. */
const SILK_TURNS = TAU + Math.PI / 4;
const SLING_START_ANGLE = Math.atan2(0.5, Math.SQRT1_2);
const SLING_SWEEP = TAU * 1.25;
const CHASE_GAP = 0.4;

type Point = readonly [number, number];

const clamp01 = (value: number) => Math.min(1, Math.max(0, value));
const smooth = (value: number) => {
  const x = clamp01(value);
  return x * x * (3 - 2 * x);
};
const lerp = (from: number, to: number, amount: number) => from + (to - from) * amount;
const mix = (a: Point, b: Point, amount: number): Point => [lerp(a[0], b[0], amount), lerp(a[1], b[1], amount)];
const polar = (radius: number, angle: number): Point => [radius * Math.cos(angle), radius * Math.sin(angle)];

/** The three dots in a row: the first on the left, the third on the right. */
function rest(dot: number): Point {
  return [(dot - 1) * ROW_GAP, 0];
}

/** Silk and gather: the dots ride a figure eight that widens, then bunch into a chase. */
function silk(dot: number, t: number): Point {
  const u = (t - REST_END) / (SILK_END - REST_END);
  const size = lerp(ROW_GAP, 0.6, smooth(u * 2));
  // At the start the offsets put the dots on the row: left lobe, crossing, right lobe.
  const spread = [Math.PI, Math.PI / 2, 0][dot] ?? 0;
  const chase = -(DOTS - 1 - dot) * (CHASE_GAP * 0.6);
  const s = u * SILK_TURNS + lerp(spread, chase, smooth((u - 0.5) / 0.5));
  const lift = 1 + Math.sin(s) ** 2;
  return [(size * Math.cos(s)) / lift, (size * Math.sin(s) * Math.cos(s)) / lift];
}

/** The angle the slingshot chase reaches at its end, for the bloom to carry on from. */
const SLING_END_ANGLE = SLING_START_ANGLE + SLING_SWEEP;

/** Slingshot: the chase runs around the rim and speeds up. */
function slingshot(dot: number, t: number): Point {
  const u = clamp01((t - SILK_END) / (SLING_END - SILK_END));
  const angle = SLING_START_ANGLE + SLING_SWEEP * u ** 1.6 - (DOTS - 1 - dot) * CHASE_GAP;
  return polar(RIM, angle);
}

/** Bloom: the chase opens into a triangle that keeps turning, slower, and draws in. */
function bloom(dot: number, t: number): Point {
  const u = (t - SLING_END) / (BLOOM_END - SLING_END);
  const gap = lerp(CHASE_GAP, TAU / DOTS, smooth(u * 1.4));
  const angle = SLING_END_ANGLE + 2.2 * u * (1 - u / 2) - (DOTS - 1 - dot) * gap;
  return polar(lerp(RIM, 0.48, smooth(u)), angle);
}

/**
 * Return the position of `dot` (0, 1 or 2) at loop progress `t`.
 *
 * Args:
 *   dot: The dot index; 0 starts on the left of the row.
 *   t: The loop progress. Any number; only its fraction counts.
 *
 * Returns:
 *   The [x, y] position, y up.
 */
export function dotAt(dot: number, t: number): Point {
  const p = t - Math.floor(t);
  if (p < REST_END) return rest(dot);
  if (p < SILK_END - 0.04) return silk(dot, p);
  if (p < SILK_END + 0.04) return mix(silk(dot, p), slingshot(dot, p), smooth((p - (SILK_END - 0.04)) / 0.08));
  if (p < SLING_END - 0.02) return slingshot(dot, p);
  if (p < SLING_END + 0.02) return mix(slingshot(dot, p), bloom(dot, p), smooth((p - (SLING_END - 0.02)) / 0.04));
  if (p < BLOOM_END - 0.06) return bloom(dot, p);
  return mix(bloom(dot, p), rest(dot), smooth((p - (BLOOM_END - 0.06)) / (1 - (BLOOM_END - 0.06))));
}
