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

/**
 * Whether the setup can show on this page: only the pages the app chrome
 * frames, since `IntakeHost` mounts in `ShellChrome` and `MobileShell` alone.
 * Keep this list in step with the bare routes in `app-shell.tsx`; a signed-out
 * public run is bare too, but a signed-out visitor never gets a setup.
 */
export function setupCoversPath(pathname: string): boolean {
  return !(
    pathname === "/login" ||
    pathname === "/desktop/link" ||
    pathname === "/mcp/authorize" ||
    pathname === "/terms" ||
    pathname === "/privacy" ||
    pathname === "/docs" ||
    pathname.startsWith("/docs/") ||
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

/**
 * Record the exit the landed splash commits to. A fade marks the splash gone
 * at once, so a setup that opens during the fade plays its own glance and
 * slide instead of the handoff entrance under a fading splash.
 *
 * Args:
 *   exit: The exit the splash starts, never "hold".
 */
export function commitSplashExit(exit: Exclude<SplashExit, "hold">): void {
  endSplash(exit === "handoff" ? "handoff" : "gone");
}

export function readSplashState(): SplashState {
  return splashState;
}

/** Subscribe to the splash state, in the shape `useSyncExternalStore` takes. */
export function subscribeSplash(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/**
 * Decide whether the setup opens with the entrance that continues the splash.
 * The splash and the setup both ask this, so the splash hands off only when
 * the setup will really take the same frame.
 *
 * Args:
 *   languageAnswered: Whether the draft already answers the language, as it
 *     does after a language-switch reload or any reload mid-tour.
 *   rerunning: Whether Settings reruns the setup.
 *   splash: The splash state when the setup mounts.
 *
 * Returns:
 *   True only for a fresh setup while the splash still plays. A splash that
 *   already left (even by a handoff nothing took) never makes a later setup
 *   skip its own glance and slide.
 */
export function playsEntrance(s: {
  languageAnswered: boolean;
  rerunning: boolean;
  splash: SplashState;
}): boolean {
  return !s.languageAnswered && !s.rerunning && s.splash === "playing";
}
