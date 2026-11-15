import type { AgentToolCall } from "./types";

/** How many of a turn's calls the auto-review stopped (SKY-706), for the turn's header. */
export function deniedCount(calls: ReadonlyArray<Pick<AgentToolCall, "autoReviewDenied">>): number {
  return calls.filter((call) => call.autoReviewDenied === true).length;
}
