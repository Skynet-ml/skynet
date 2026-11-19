/*
 * Position logic of the replay viewer (SKY-726). The server pages one
 * agent's steps oldest first; the viewer keeps what it loaded and asks for
 * the next page only when the user steps past the last loaded step.
 */

import type { ReplayStepPage } from "@/shared/lib/api";

/** Steps the viewer asks for in one page. */
export const REPLAY_PAGE_SIZE = 20;

/** The number of steps the server holds for an agent, or 0 when it is unknown. */
export function agentStepCount(
  page: Pick<ReplayStepPage, "agents">,
  agentId: string | null,
): number {
  return page.agents.find((agent) => agent.agent_id === agentId)?.step_count ?? 0;
}

/** Whether a step before `index` exists. */
export function canStepBack(index: number): boolean {
  return index > 0;
}

/** Whether a step after `index` is loaded, or the server has more. */
export function canStepForward(index: number, loaded: number, hasMore: boolean): boolean {
  return index + 1 < loaded || hasMore;
}

/** Whether a step forward from `index` needs the next page first. */
export function needsNextPage(index: number, loaded: number, hasMore: boolean): boolean {
  return hasMore && index + 1 >= loaded;
}

/** A tree id short enough to read; the full id stays in the title. */
export function shortTreeId(treeId: string): string {
  return treeId.slice(0, 12);
}
