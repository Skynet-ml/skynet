/**
 * One entrance on a first login: the app-load splash and the setup's opening
 * are the same screen. When the splash lands on the human and a setup is
 * waiting, the splash hands the screen to the setup with no fade, and the
 * setup starts on the same frame (figure above the wordmark, same ground).
 */

export type SessionStatus = "loading" | "authenticated" | "unauthenticated";

/** What the splash does once its figure lands. */
export type SplashExit = "fade" | "handoff" | "hold";

/** The longest the landed splash waits for the account's setup state. */
export const SPLASH_HOLD_MAX_MS = 1500;

/**
 * Pick how the landed splash leaves.
 *
 * Args:
 *   session: The session status.
 *   loaded: Whether the account's setup state is known.
 *   setupShows: Whether the setup will cover this page.
 *   holdExpired: Whether the splash already waited `SPLASH_HOLD_MAX_MS`.
 *
 * Returns:
 *   "handoff" when the setup takes over, "hold" while the state is still
 *   coming, else "fade".
 */
export function splashExit(s: {
  session: SessionStatus;
  loaded: boolean;
  setupShows: boolean;
  holdExpired: boolean;
}): SplashExit {
  if (s.setupShows) return "handoff";
  // A signed-out visitor never gets a setup, so only a signed-in (or still unknown) one waits.
  const unknown = s.session === "loading" || (s.session === "authenticated" && !s.loaded);
  return unknown && !s.holdExpired ? "hold" : "fade";
}

/** The setup never covers the sign-in or public pages. */
export function setupCoversPath(pathname: string): boolean {
  return !(
    pathname === "/login" ||
    pathname === "/terms" ||
    pathname === "/privacy" ||
    pathname.startsWith("/share/")
  );
}

/** "playing" until the splash leaves; then whether it handed off or faded. */
export type SplashState = "playing" | "handoff" | "gone";

let splashState: SplashState = "playing";
const listeners = new Set<() => void>();

/** Record how the splash left, once per page load. */
export function endSplash(how: Exclude<SplashState, "playing">): void {
  if (splashState !== "playing") return;
  splashState = how;
  listeners.forEach((l) => l());
}

export function readSplashState(): SplashState {
  return splashState;
}

/** Subscribe to the splash state, in the shape `useSyncExternalStore` takes. */
export function subscribeSplash(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
