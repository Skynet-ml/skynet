import { formatMsg, msg } from "@/shared/lib/messages";

/**
 * The server's fixed opening for an agent chat whose input (a repository, a
 * dataset) is over the opening budget: no model ran, so the client words
 * the reply itself, in the user's language. Every agent stream that can
 * open by itself recognizes it through `kickoffOpening`.
 */
const KICKOFF_OVERSIZED_EVENT = "kickoff_oversized";

/** The opening's text when `event` is the fixed opening; `null` otherwise. */
export function kickoffOpening(event: string, data: Record<string, unknown>): string | null {
  if (event !== KICKOFF_OVERSIZED_EVENT) return null;
  const name = typeof data.name === "string" ? data.name.trim() : "";
  return data.subject === "repo" && name
    ? formatMsg("agent.kickoff.oversized_repo", { name })
    : msg("agent.kickoff.oversized_data");
}
