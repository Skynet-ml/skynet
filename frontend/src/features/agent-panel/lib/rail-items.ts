/** Build the user message rail's items from the chat's messages. */

import { splitLineComments } from "@/shared/ui/agent/line-comments";
import type { AgentMessage } from "@/shared/ui/agent/types";
import type { RailItem } from "@/shared/ui/agent/user-message-rail";

/**
 * Make one rail item for each user message that the chat shows.
 *
 * Args:
 *   messages: The chat's messages.
 *
 * Returns:
 *   For each user message: its index and its first line without the line comments.
 */
export function railItemsOf(messages: readonly AgentMessage[]): RailItem[] {
  const items: RailItem[] = [];
  messages.forEach((m, index) => {
    // The transcript drops a hidden user message (an internal request), so it gets no mark.
    if (m.role !== "user" || m.hidden) return;
    items.push({ index, label: splitLineComments(m.content).rest.trim().split("\n")[0] ?? "" });
  });
  return items;
}
