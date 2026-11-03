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

/**
 * How an edit of the message at `messageIndex` targets its stored turn. A
 * new edit of the latest user turn while an earlier edit is unconfirmed
 * targets that earlier edit's turn, as a retry does: the displayed text was
 * never stored when the earlier edit failed before the server saved it, so
 * a target built from it finds nothing, and the old turn stays.
 */
export function editMode(
  messages: ReadonlyArray<{ role: string; content: string }>,
  messageIndex: number,
  unsavedEdit: { text: string; occurrence: number } | null,
): { regenerate: boolean; editUserTurn: { text: string; occurrence: number } | null } {
  const isLatestUser = messages.slice(messageIndex + 1).every((m) => m.role !== "user");
  if (unsavedEdit && isLatestUser) return retryMode(unsavedEdit);
  return {
    regenerate: false,
    editUserTurn: messages[messageIndex]?.role === "user" ? editTarget(messages, messageIndex) : null,
  };
}

/**
 * How a retry resends the latest user turn. An unconfirmed edit resends its
 * edit target: `regenerate` alone would delete only the latest stored turn,
 * and the old edited turn and its replies would come back after a reload.
 * It also sends `regenerate`, because the server can save the edit before
 * the stream breaks. Then the old turn is gone, and the server replaces the
 * saved edit when it is the latest stored turn.
 */
export function retryMode(unsavedEdit: { text: string; occurrence: number } | null): {
  regenerate: boolean;
  editUserTurn: { text: string; occurrence: number } | null;
} {
  return unsavedEdit
    ? { regenerate: true, editUserTurn: unsavedEdit }
    : { regenerate: true, editUserTurn: null };
}
