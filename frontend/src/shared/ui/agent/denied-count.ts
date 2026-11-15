import type { AgentToolCall } from "./types";

/** How many of a turn's calls the auto-review stopped (SKY-706), for the turn's header. */
export function deniedCount(calls: ReadonlyArray<Pick<AgentToolCall, "autoReviewDenied">>): number {
  return calls.filter((call) => call.autoReviewDenied === true).length;
}

/**
 * Set or clear the denial flag of call ``id`` in the last assistant message.
 * The header counts only calls that did not run, so "Approve anyway" clears it.
 */
export function withAutoReviewDenied<M extends { role: string; toolCalls?: AgentToolCall[] }>(
  prev: M[],
  id: string,
  denied: boolean,
): M[] {
  const last = prev[prev.length - 1];
  const call = last?.role === "assistant" ? last.toolCalls?.find((t) => t.id === id) : undefined;
  if (!last || !call || (call.autoReviewDenied === true) === denied) return prev;
  const next = prev.slice();
  next[next.length - 1] = {
    ...last,
    toolCalls: last.toolCalls?.map((t) => (t.id === id ? { ...t, autoReviewDenied: denied } : t)),
  };
  return next;
}
