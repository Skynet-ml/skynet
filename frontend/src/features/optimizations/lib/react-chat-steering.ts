import type { AgentMessage } from "../../../shared/ui/agent/types";

/** Matches ServeChatRequest.user_message's AGENT_MAX_TEXT limit. */
export const REACT_CHAT_USER_MESSAGE_MAX_CHARS = 2000;

/** Only the running non-protected turn announces its requested mailbox. */
export function reactChatCanSteer(announcedKey: string | undefined, requestedKey: string): boolean {
  return Boolean(requestedKey && announcedKey === requestedKey);
}

/** Keep the reply so far, then show the applied steer as a new user turn. */
export function appendReactChatSteer(messages: AgentMessage[], text: string): AgentMessage[] {
  if (!text.trim()) return messages;
  const last = messages[messages.length - 1];
  const kept = last?.role === "assistant" && !last.content && !last.toolCalls?.length
    ? messages.slice(0, -1)
    : messages;
  return [
    ...kept,
    { role: "user", content: text, createdAt: Date.now() },
    { role: "assistant", content: "", toolCalls: [] },
  ];
}

/** Remove the reply prefix repeated by a program after it accepts a steer. */
export function removeReplyBeforeSteer(reply: string, beforeSteer: string): string {
  if (!beforeSteer || !reply.startsWith(beforeSteer)) return reply;
  return reply.slice(beforeSteer.length).trimStart();
}
