/** Open a minimized floating chat when its turn needs the user. */

import type { ChatFloat } from "../hooks/use-wizard-state";

/**
 * Expand a minimized floating chat when the chat starts to need the user.
 *
 * A minimized chat hides its thread, so an error, a question or an approval
 * request would not show. Only the start of the need expands the chat, so the
 * user can minimize it again while the need stays.
 *
 * Args:
 *   was: The chat needed the user at the last check.
 *   needs: The chat needs the user now.
 *   float: The floating chat, or null when the chat is docked.
 */
export function expandOnAttention(was: boolean, needs: boolean, float: ChatFloat | null): void {
  if (needs && !was && float && !float.expanded) float.onExpand(true);
}
