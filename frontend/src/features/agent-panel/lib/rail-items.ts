/** Build the user message rail's items from the chat's messages. */

import { splitLineComments } from "@/shared/ui/agent/line-comments";
import type { AgentMessage } from "@/shared/ui/agent/types";
import type { RailItem } from "@/shared/ui/agent/user-message-rail";

/**
 * Make one rail item for each user message.
 *
 * Args:
 *   messages: The chat's messages.
 *   outputsOf: The files that one reply changed.
 *
 * Returns:
 *   For each user message: its first line without the line comments, the
 *   start of the first answer with text, and the files the turn changed.
 */
export function railItemsOf(
  messages: readonly AgentMessage[],
  outputsOf: (message: AgentMessage) => readonly string[],
): RailItem[] {
  const items: RailItem[] = [];
  messages.forEach((m, index) => {
    if (m.role === "user") {
      items.push({
        index,
        label: splitLineComments(m.content).rest.trim().split("\n")[0] ?? "",
        preview: null,
        outputs: [],
      });
      return;
    }
    const item = items.at(-1);
    if (!item) return;
    if (!item.preview && m.content.trim()) item.preview = m.content.trim().slice(0, 400);
    const outputs = outputsOf(m).filter((path) => !item.outputs.includes(path));
    if (outputs.length) item.outputs = [...item.outputs, ...outputs];
  });
  return items;
}
