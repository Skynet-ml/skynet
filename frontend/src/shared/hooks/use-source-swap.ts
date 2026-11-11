"use client";

import * as React from "react";

/** Shortest time a skeleton stays up after a Local/Web switch. A shorter one reads as a flicker. */
export const SWAP_MIN_MS = 450;

/**
 * True from the moment `source` changes until {@link SWAP_MIN_MS} later, so a
 * screen that follows the Local/Web pick shows its skeleton for a steady beat
 * even when the other side answers at once. The first render returns false:
 * the first load has its own loading state.
 */
export function useSourceSwap(source: unknown): boolean {
  const [shown, setShown] = React.useState(source);
  const [swapping, setSwapping] = React.useState(false);
  if (shown !== source) {
    setShown(source);
    setSwapping(true);
  }
  // `shown` restarts the timer, so a quick double switch waits for the last one.
  React.useEffect(() => {
    if (!swapping) return;
    const timer = window.setTimeout(() => setSwapping(false), SWAP_MIN_MS);
    return () => window.clearTimeout(timer);
  }, [swapping, shown]);
  return swapping;
}

/**
 * Return a check that tells whether a reply asked for `side` still matches the
 * current Local/Web pick. A slow reply from the side the user left must not
 * replace the rows of the side they are on.
 */
export function useIsCurrentSide<T>(side: T): (asked: T) => boolean {
  const current = React.useRef(side);
  React.useLayoutEffect(() => {
    current.current = side;
  }, [side]);
  return React.useCallback((asked: T) => asked === current.current, []);
}
