/** Find the user turns whose reply came from a new model, for the chat's model-changed notice. */

import type { AgentMessage } from "@/shared/ui/agent/types";

/** The model of the turn before, and of this turn. */
export interface ModelChange {
  from: string;
  to: string;
}

/**
 * Map each user message whose turn changed the model to the change.
 *
 * Only replies carry a model, so each turn takes the model of its last reply
 * that names one. A turn with no model yet (still streaming, or stopped)
 * shows no notice and does not reset the model to compare with. The
 * transcript draws no row for a hidden user message, so a change on its turn
 * waits for the next shown user message.
 *
 * Args:
 *   messages: The chat's messages.
 *
 * Returns:
 *   The change by the index of the user message that starts the turn.
 */
export function modelChanges(messages: readonly AgentMessage[]): Map<number, ModelChange> {
  const turns: Array<{ index: number; model: string | null; hidden: boolean }> = [];
  messages.forEach((m, index) => {
    if (m.role === "user") {
      turns.push({ index, model: null, hidden: !!m.hidden });
      return;
    }
    const turn = turns.at(-1);
    const model = m.model ?? m.servedModel;
    if (turn && model) turn.model = model;
  });
  const changes = new Map<number, ModelChange>();
  let prev: string | null = null;
  let pending = null as ModelChange | null;
  for (const turn of turns) {
    if (turn.model) {
      if (prev && turn.model !== prev) pending = { from: pending?.from ?? prev, to: turn.model };
      prev = turn.model;
    }
    if (turn.hidden || !pending) continue;
    if (pending.from !== pending.to) changes.set(turn.index, pending);
    pending = null;
  }
  return changes;
}
