import type { AgentMessage, QuestionChoice } from "@/shared/ui/agent/types";

export const ASK_USER_TOOL = "ask_user";

/**
 * The answers the agent offered with the question that ended the chat, or
 * null once the user replied, the turn still runs, or the turn asked nothing.
 */
export function askedChoices(messages: readonly AgentMessage[]): QuestionChoice[] | null {
  const last = messages[messages.length - 1];
  if (last?.role !== "assistant") return null;
  const call = [...(last.toolCalls ?? [])]
    .reverse()
    .find((c) => c.tool === ASK_USER_TOOL && c.status === "done");
  const result = call?.payload?.result;
  const raw =
    result && typeof result === "object" ? (result as Record<string, unknown>).options : null;
  if (!Array.isArray(raw)) return null;
  const options = raw.flatMap((item): QuestionChoice[] => {
    if (!item || typeof item !== "object") return [];
    const { label, description } = item as Record<string, unknown>;
    if (typeof label !== "string" || !label.trim()) return [];
    return [
      {
        label: label.trim(),
        description: typeof description === "string" ? description.trim() : "",
      },
    ];
  });
  return options.length >= 2 ? options : null;
}
