// The effort slider's stops and keys. Kept free of React so `node --test` covers them.

/**
 * A saved effort, with a retired level read as "max". Codex's "ultra" and
 * Claude Code's "ultracode" only added subagents, which Skynet keeps off; the
 * backend also runs either one at the model's top level.
 */
export function liveEffort(effort: string | null): string | null {
  return effort === "ultra" || effort === "ultracode" ? "max" : effort;
}

/**
 * The retired top level a CLI model's picker shows greyed out, or `null` for
 * a model that is not a CLI model. It is never a stop, so it is never sent.
 */
export function retiredLevel(model: string | null): "ultra" | "ultracode" | null {
  if (model?.startsWith("subscription/claude/")) return "ultracode";
  if (model?.startsWith("subscription/codex/")) return "ultra";
  return null;
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

/** Whether a turn sends fast mode: only to a local CLI model (`sent` is the
 * model id the request runs). The backend drops it on a model without it. */
export function sentFastMode(fast: boolean, sent: string | null): boolean {
  return fast && !!sent?.startsWith("subscription/");
}
