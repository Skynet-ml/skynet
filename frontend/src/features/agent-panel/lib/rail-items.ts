/** Build the user message rail's items from the chat's messages. */

import { splitLineComments } from "@/shared/ui/agent/line-comments";
import type { AgentMessage } from "@/shared/ui/agent/types";
import type { RailItem } from "@/shared/ui/agent/user-message-rail";

/**
 * Cache a reply's changed files on its tool calls.
 *
 * A streamed token makes a new message object but keeps its tool calls, so
 * the files are read again only when the tool calls change.
 *
 * Args:
 *   read: Read the files that one reply changed.
 *
 * Returns:
 *   `read` with a cache. A message with no tool calls changed no files.
 */
export function outputsByToolCalls(
  read: (message: AgentMessage) => readonly string[],
): (message: AgentMessage) => readonly string[] {
  const cache = new WeakMap<object, readonly string[]>();
  return (message) => {
    const calls = message.toolCalls;
    if (!calls?.length) return [];
    let paths = cache.get(calls);
    if (!paths) {
      paths = read(message);
      cache.set(calls, paths);
    }
    return paths;
  };
}

/**
 * Make one rail item for each user message that the chat shows.
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
    // The transcript drops a hidden user message (an internal request), so it gets
    // no mark. Its reply adds to the last shown turn, or to none before the first.
    if (m.role === "user" && m.hidden) return;
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
