// Where the transcript draws each compaction row. Only type imports here, so
// `node --test` loads the file as it is.
//
// A compaction entry is stored on the first message it kept, because the
// next turn sends the history from there. The row shows where the
// compaction ran in time: a threshold compaction runs at the start of a
// turn, after its user message and before the answer; a /compact runs after
// the last message of the chat.

import type { AgentMessage, ChatCompaction } from "./types";

/** One compaction row and the message it shows after. */
export interface CompactionSlot {
  entry: ChatCompaction;
  /** The index of the message that stores the entry; unique, so a React key. */
  anchor: number;
  /** The index of the message the row shows after. */
  after: number;
}

/** The compaction rows of a chat, in message order. */
export function compactionSlots(messages: readonly AgentMessage[]): CompactionSlot[] {
  const slots: CompactionSlot[] = [];
  messages.forEach((message, anchor) => {
    const entry = message.compaction;
    if (!entry) return;
    // The kept turns are the non-empty messages from the anchor on, as
    // chatWindow counts them; the last one was the newest when it ran.
    let lastKept = anchor;
    let kept = 0;
    for (let i = anchor; i < messages.length && kept < entry.keptTurns; i++) {
      if (!messages[i]!.content.trim()) continue;
      kept++;
      lastKept = i;
    }
    // A /compact ran after the whole answer. Empty messages after the last
    // kept one (a tool-only answer) still came before it.
    if (entry.reason === "manual") {
      while (lastKept + 1 < messages.length && !messages[lastKept + 1]!.content.trim()) lastKept++;
    }
    let after = lastKept;
    if (entry.reason === "threshold" && entry.keptTurns > 0) {
      const trigger = messages.findIndex((m, i) => i > lastKept && m.role === "user");
      if (trigger >= 0) after = trigger;
    }
    slots.push({ entry, anchor, after });
  });
  return slots;
}

/** Where a row shows in a transcript block (a user message and its answer). */
export type SlotPlace = "before" | "afterUser" | "after";

/**
 * The block and place of each row. `blocks` lists each block's user index
 * (or null) and last message index. A row after a message in no block (a
 * hidden turn) shows before the next block; a row past every block shows
 * after the last one.
 */
export function placeSlots(
  slots: readonly CompactionSlot[],
  blocks: ReadonlyArray<{ key: string; user: number | null; last: number }>,
): Map<string, Array<{ slot: CompactionSlot; place: SlotPlace }>> {
  const out = new Map<string, Array<{ slot: CompactionSlot; place: SlotPlace }>>();
  const add = (key: string, slot: CompactionSlot, place: SlotPlace) =>
    out.set(key, [...(out.get(key) ?? []), { slot, place }]);
  for (const slot of slots) {
    const block = blocks.find((b) => b.last >= slot.after);
    if (!block) {
      const tail = blocks[blocks.length - 1];
      if (tail) add(tail.key, slot, "after");
      continue;
    }
    if (block.user === slot.after) add(block.key, slot, "afterUser");
    else if (block.user !== null && slot.after < block.user) add(block.key, slot, "before");
    else add(block.key, slot, "after");
  }
  return out;
}
