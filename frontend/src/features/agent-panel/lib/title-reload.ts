import * as React from "react";

import { announceWalletChange } from "@/shared/lib/api";

// The server waits up to 10 s for the title after the turn, and the title
// model call can take up to 30 s. A stream can close before either ends (a
// stop, an error or the tail wait), while the server still saves the title.
// So read it at 5 s, and once more when the model call must be over.
export const TITLE_RELOAD_GAPS_MS = [5_000, 30_000];

/**
 * Read a chat's saved title again after its stream closed without one.
 * `reload` resolves true when it found the title; then no later read runs.
 * The last read refreshes the balance, as the title call is billed late.
 * Aborting `signal` (the panel unmounts) clears the waiting timer.
 */
export function scheduleTitleReloads(reload: () => Promise<boolean>, signal: AbortSignal): void {
  const run = (step: number) => {
    if (signal.aborted || step >= TITLE_RELOAD_GAPS_MS.length) return;
    const stop = () => clearTimeout(timer);
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", stop);
      void reload()
        .catch(() => false)
        .then((found) => {
          if (!found && step + 1 < TITLE_RELOAD_GAPS_MS.length) {
            run(step + 1);
            return;
          }
          // The title call is billed after the turn's own refresh. The server
          // meters it before it saves the title, and by the last read the
          // model call is over, so the balance is now final.
          if (!signal.aborted) announceWalletChange();
        });
    }, TITLE_RELOAD_GAPS_MS[step]);
    signal.addEventListener("abort", stop, { once: true });
  };
  run(0);
}

/**
 * Give a callback that reads a chat's title again after its stream closed
 * without one. The server can still save the title then (a stop, an error,
 * or a title later than the tail wait). The reads stop on unmount.
 */
export function useTitleReloads(
  reloadTitle: (id: string, cutTitle: string) => Promise<boolean>,
): (id: string, cutTitle: string) => void {
  const reloadsRef = React.useRef<AbortController | null>(null);
  React.useEffect(() => {
    const reloads = new AbortController();
    reloadsRef.current = reloads;
    return () => reloads.abort();
  }, []);
  return React.useCallback(
    (id: string, cutTitle: string) => {
      const reloads = reloadsRef.current;
      if (reloads) scheduleTitleReloads(() => reloadTitle(id, cutTitle), reloads.signal);
    },
    [reloadTitle],
  );
}
