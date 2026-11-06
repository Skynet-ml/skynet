/**
 * Turns the app sends an agent by itself, such as a failed setup check, open
 * with this prefix. The backend's prompt names the same text
 * (``AUTOMATIC_TURN_PREFIX`` in ``generalist.py``), so the agent knows the
 * user did not type them.
 */
export const AUTOMATIC_TURN_PREFIX = "[Automatic setup check]";

/**
 * Build an automatic turn's message.
 *
 * Args:
 *   body: What the turn reports.
 *
 * Returns:
 *   The message, prefixed.
 */
export function automaticTurn(body: string): string {
  return `${AUTOMATIC_TURN_PREFIX}\n${body}`;
}

/**
 * Read an automatic turn's body back from a message.
 *
 * Args:
 *   content: A user-position message.
 *
 * Returns:
 *   The body, or null when the user wrote the message.
 */
export function automaticTurnBody(content: string): string | null {
  return content.startsWith(AUTOMATIC_TURN_PREFIX)
    ? content.slice(AUTOMATIC_TURN_PREFIX.length).trim()
    : null;
}
