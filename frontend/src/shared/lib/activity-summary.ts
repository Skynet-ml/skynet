/**
 * What a step of an agent's turn did, as Codex sorts them: it read something,
 * searched, edited code, ran a check or a command, changed something in the
 * account, or used a tool the transcript knows nothing more about.
 */
export type ActivityKind = "edit" | "act" | "check" | "search" | "read" | "run" | "tool";

export type ActivityStatus = "running" | "done" | "error";

/** The one summary line over a burst of steps. */
export interface ActivitySummary {
  /** Each kind present, in the order the line names them, and how many steps it has. */
  parts: Array<{ kind: ActivityKind; count: number }>;
  /** The kind whose icon leads the line; null for an empty burst. */
  lead: ActivityKind | null;
  running: boolean;
  failed: boolean;
}

// Codex's fixed order: account actions, file changes, exploration, checks and
// commands, then other tools ("Edited files, read files, ran commands").
const ORDER: readonly ActivityKind[] = ["act", "edit", "search", "read", "check", "run", "tool"];

/**
 * Summarize a burst of steps into the parts of its one-line summary.
 *
 * Args:
 *   steps: The burst's steps, in the order they ran.
 *
 * Returns:
 *   The kinds to name with their counts, the lead kind, and whether any step
 *   is still running or failed.
 */
export function summarizeActivity(
  steps: ReadonlyArray<{ kind: ActivityKind; status: ActivityStatus }>,
): ActivitySummary {
  const counts = new Map<ActivityKind, number>();
  for (const step of steps) counts.set(step.kind, (counts.get(step.kind) ?? 0) + 1);
  const parts = ORDER.filter((kind) => counts.has(kind)).map((kind) => ({
    kind,
    count: counts.get(kind) ?? 0,
  }));
  return {
    parts,
    lead: parts[0]?.kind ?? null,
    running: steps.some((s) => s.status === "running"),
    failed: steps.some((s) => s.status === "error"),
  };
}

/**
 * Join already-translated summary phrases into one line, capitalizing the first.
 *
 * Args:
 *   phrases: One lower-case phrase per kind, e.g. "edited a file".
 *   locale: The UI locale, for the capital.
 *
 * Returns:
 *   The line, e.g. "Edited a file, read results".
 */
export function joinSummary(phrases: readonly string[], locale: string): string {
  const line = phrases.join(", ");
  return line.charAt(0).toLocaleUpperCase(locale) + line.slice(1);
}

/**
 * Whether a burst shows its rows.
 *
 * Without ``stayOpen`` a burst folds on its own once it finished; in the agent
 * panel that collapsed a coding run's "Edited run.py +5 −0" rows into "Edited
 * files, ran a check" the moment its check passed.
 *
 * Args:
 *   choice: What the reader last clicked, or null when they never did.
 *   state: Whether the burst is live, has a step running, or stays open.
 *
 * Returns:
 *   True when the rows show.
 */
export function activityGroupOpen(
  choice: boolean | null,
  state: { live: boolean; running: boolean; stayOpen: boolean },
): boolean {
  return choice ?? (state.stayOpen || state.live || state.running);
}
