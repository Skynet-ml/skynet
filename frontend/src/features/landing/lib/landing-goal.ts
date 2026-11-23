/**
 * The goal a signed-out visitor writes on the landing page.
 *
 * It is kept in localStorage, not sessionStorage: an OAuth sign-up leaves the
 * site and can come back in a new tab. The first wizard step takes it once and
 * clears it (SKY-922). A goal older than an hour is dropped, so a later account
 * on a shared browser does not get another visitor's goal.
 */

const LANDING_GOAL_KEY = "skynet.landing-goal";

// A goal is one prompt, not a document; the cap keeps a paste from filling storage.
const MAX_GOAL_LENGTH = 4000;

// Long enough for a sign-up with an email code; short enough for a shared browser.
const GOAL_TTL_MS = 60 * 60 * 1000;

type GoalStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/** The browser's localStorage, or null where there is none or access throws. */
function browserStorage(): GoalStorage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** Keep the visitor's goal for the run setup after sign-in. */
export function saveLandingGoal(
  goal: string,
  storage = browserStorage(),
  now = Date.now(),
): void {
  const trimmed = goal.trim().slice(0, MAX_GOAL_LENGTH);
  try {
    if (trimmed) storage?.setItem(LANDING_GOAL_KEY, JSON.stringify({ goal: trimmed, savedAt: now }));
    else storage?.removeItem(LANDING_GOAL_KEY);
  } catch {}
}

/** Return the kept goal once and clear it, or "" when there is none or it is stale. */
export function takeLandingGoal(storage = browserStorage(), now = Date.now()): string {
  try {
    const raw = storage?.getItem(LANDING_GOAL_KEY);
    if (!raw) return "";
    storage?.removeItem(LANDING_GOAL_KEY);
    const { goal, savedAt } = JSON.parse(raw) as { goal?: unknown; savedAt?: unknown };
    if (typeof goal !== "string" || typeof savedAt !== "number") return "";
    return now - savedAt <= GOAL_TTL_MS ? goal : "";
  } catch {
    return "";
  }
}
