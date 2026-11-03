/**
 * The user message at `messageIndex`, as the backend finds its stored copy:
 * its text, and how many earlier user messages in the chat have the same
 * text. The backend matches by text, not position, because the chat and the
 * store can each hold a turn the other lacks (a refused send, a steered
 * message that never reached the chat).
 */
export function editTarget(
  messages: ReadonlyArray<{ role: string; content: string }>,
  messageIndex: number,
): { text: string; occurrence: number } {
  const text = messages[messageIndex]?.content ?? "";
  const occurrence = messages
    .slice(0, messageIndex)
    .filter((m) => m.role === "user" && m.content === text).length;
  return { text, occurrence };
}
