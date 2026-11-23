/**
 * The goal a signed-out visitor writes on the landing page.
 *
 * It is kept in localStorage, not sessionStorage: an OAuth sign-up leaves the
 * site and can come back in a new tab. The first wizard step takes it once and
 * clears it (SKY-922).
 */

const LANDING_GOAL_KEY = "skynet.landing-goal";

// A goal is one prompt, not a document; the cap keeps a paste from filling storage.
const MAX_GOAL_LENGTH = 4000;

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
export function saveLandingGoal(goal: string, storage = browserStorage()): void {
  const trimmed = goal.trim().slice(0, MAX_GOAL_LENGTH);
  try {
    if (trimmed) storage?.setItem(LANDING_GOAL_KEY, trimmed);
    else storage?.removeItem(LANDING_GOAL_KEY);
  } catch {}
}

/** Return the kept goal once and clear it, or "" when there is none. */
export function takeLandingGoal(storage = browserStorage()): string {
  try {
    const goal = storage?.getItem(LANDING_GOAL_KEY) ?? "";
    if (goal) storage?.removeItem(LANDING_GOAL_KEY);
    return goal;
  } catch {
    return "";
  }
}
