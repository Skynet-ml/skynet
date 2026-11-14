/**
 * When the chat retries a turn by itself after a model capacity error, kept
 * apart from the button so the tests pin the rule without a DOM.
 */

// Codex's backoff: each capacity failure in a row waits longer, and after the
// fourth the user must retry by hand.
const RETRY_DELAYS_SECONDS = [10, 30, 120, 300] as const;

/**
 * The wait before the automatic retry.
 *
 * Args:
 *   failuresInARow: Capacity failures before this one, with no new message between them.
 *
 * Returns:
 *   Seconds until the chat retries, or null when only a manual retry is left.
 */
export function capacityRetryDelay(failuresInARow: number): number | null {
  return RETRY_DELAYS_SECONDS[Math.max(0, failuresInARow)] ?? null;
}

/**
 * Whole seconds left until a deadline, never below zero.
 *
 * Args:
 *   deadline: The retry time, in epoch milliseconds.
 *   now: The current time, in epoch milliseconds.
 *
 * Returns:
 *   The seconds to show on the button.
 */
export function secondsLeft(deadline: number, now: number): number {
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}
