// The effort slider's stops and keys. Kept free of React so `node --test` covers them.

/**
 * A saved effort, with a retired level read as "max". Codex's "ultra" and
 * Claude Code's "ultracode" only added subagents, which Skynet keeps off; the
 * backend also runs either one at the model's top level. "none" now uses
 * the model default instead of a separate reasoning-off stop.
 */
export function liveEffort(effort: string | null): string | null {
  if (effort === "none") return null;
  return effort === "ultra" || effort === "ultracode" ? "max" : effort;
}

/**
 * The level a CLI model runs for a saved effort, as the backend maps it: Max
 * (and a retired level) on a model whose top level is lower runs at that top
 * level. A level the CLI does not take ("none", or one saved for another
 * model) runs the CLI's default, `null`.
 */
export function cliEffort(effort: string | null, levels: readonly string[]): string | null {
  const live = liveEffort(effort);
  if (live === "max" && !levels.includes("max")) return levels.at(-1) ?? null;
  return live && levels.includes(live) ? live : null;
}

/** The slider's stops, weakest first: the model default (`null`), then each level. */
export function sliderStops(efforts: readonly string[]): Array<string | null> {
  return [null, ...efforts];
}

/**
 * The stop a key press moves the slider to, or `undefined` for a key the
 * slider does not handle. In a right-to-left page the track runs from the
 * right, so Left moves up and Right moves down; Up and Down never flip.
 */
export function stepEffort(
  efforts: readonly string[],
  current: string | null,
  key: string,
  rtl: boolean,
): string | null | undefined {
  const stops = sliderStops(efforts);
  // A saved level the model does not take sits on the default stop.
  const at = Math.max(0, stops.indexOf(current));
  const up = rtl ? "ArrowLeft" : "ArrowRight";
  const down = rtl ? "ArrowRight" : "ArrowLeft";
  let next: number;
  if (key === up || key === "ArrowUp") next = at + 1;
  else if (key === down || key === "ArrowDown") next = at - 1;
  else if (key === "Home") next = 0;
  else if (key === "End") next = stops.length - 1;
  else return undefined;
  return stops[Math.min(stops.length - 1, Math.max(0, next))];
}

/** How far along the track `level` sits, from 0 (default) to 1 (top stop). */
export function effortFill(efforts: readonly string[], level: string | null): number {
  const stops = sliderStops(efforts);
  if (stops.length < 2) return 0;
  return Math.max(0, stops.indexOf(level)) / (stops.length - 1);
}

/**
 * The fill's motion for a fill fraction: a light band sweeps along the fill,
 * slow and faint at low effort, fast and bright at the top stop. `null` at the
 * model default, where the fill is only the cap under the thumb.
 */
export function fillMotion(fill: number): { seconds: number; strength: number } | null {
  if (!(fill > 0)) return null;
  const t = Math.min(1, fill);
  const round = (n: number) => Math.round(n * 100) / 100;
  return { seconds: round(5 - 3.6 * t), strength: round(0.18 + 0.2 * t) };
}

/**
 * How charged the slider looks at `level`. `heat` runs from 0 (the default and
 * the lowest level, a plain neutral fill) to 1 (the top level). Only the top
 * two levels throw sparks and pulse, the top one more. Each step up flashes
 * and sprays sparks (`spray`). Reduced motion keeps only the heat, so the
 * color and the still glow still show the level.
 */
export function effortCharge(
  efforts: readonly string[],
  level: string | null,
  reducedMotion: boolean,
): { heat: number; sparks: number; pulse: boolean; spray: number } {
  const levels = efforts.length;
  const at = Math.max(0, sliderStops(efforts).indexOf(level));
  const heat = levels < 2 || at < 2 ? 0 : Math.round(((at - 1) / (levels - 1)) * 100) / 100;
  if (reducedMotion) return { heat, sparks: 0, pulse: false, spray: 0 };
  // A model with only two levels has no middle, so only its top level sparks.
  const sparks = at === levels && levels >= 2 ? 6 : at === levels - 1 && levels >= 3 ? 2 : 0;
  const spray = heat > 0 ? 2 + Math.round(heat * 4) : 0;
  return { heat, sparks, pulse: sparks > 0, spray };
}
/** Send fast mode for local CLI or OpenRouter turns, including the server default.
 * The backend checks the resolved model's support before applying it. */
export function sentFastMode(fast: boolean, sent: string | null): boolean {
  return (
    fast && (sent === null || sent.startsWith("subscription/") || sent.startsWith("openrouter/"))
  );
}
