/**
 * The setup check of a repository run's draft: the state the pane toolbar
 * shows, and the line of optimize.toml that each parser error points to.
 */

/** One parser error and the table and key that caused it. */
export interface SetupCheckError {
  section: string | null;
  key: string | null;
  message: string;
}

/** A finished check and the optimize.toml text it read. */
export interface SetupCheckRun {
  text: string;
  ok: boolean;
  errors: SetupCheckError[];
}

export type SetupCheckState = "idle" | "running" | "passed" | "failed" | "stale";

/**
 * Name the state of the setup check.
 *
 * Args:
 *   run: The newest finished check, or null.
 *   running: A check runs now.
 *   text: The optimize.toml the draft holds now.
 *
 * Returns:
 *   "running" while a check runs, "idle" before the first, "stale" when the
 *   file changed after the last, else its outcome.
 */
export function setupCheckState(run: SetupCheckRun | null, running: boolean, text: string): SetupCheckState {
  if (running) return "running";
  if (!run) return "idle";
  if (run.text !== text) return "stale";
  return run.ok ? "passed" : "failed";
}

const HEADER = /^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$/;
const KEY = /^\s*("?)([A-Za-z0-9_.-]+)\1\s*=/;

function tableName(raw: string): string {
  return raw
    .split(".")
    .map((part) => part.trim().replace(/^"(.*)"$/, "$1"))
    .join(".");
}

/**
 * Find the line of optimize.toml that a parser error points to.
 *
 * Args:
 *   text: The file.
 *   error: The error; a null section means the top level, before any table.
 *
 * Returns:
 *   The 1-based line of the key, or of the table header when the error
 *   names no key or the key is missing; null when the file has neither.
 */
export function errorLine(text: string, error: Pick<SetupCheckError, "section" | "key">): number | null {
  const lines = text.split("\n");
  let current: string | null = null;
  let header: number | null = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    const head = HEADER.exec(line);
    if (head) {
      if (current === error.section && header !== null) break;
      current = tableName(head[1]!);
      if (current === error.section) header = i + 1;
      continue;
    }
    if (current !== error.section || error.key === null) continue;
    if (KEY.exec(line)?.[2] === error.key) return i + 1;
  }
  return header;
}

/**
 * Write a failed check as one message for the setup agent.
 *
 * Args:
 *   run: The failed check.
 *   intro: The opening sentence, in the user's language.
 *
 * Returns:
 *   The intro, then one line per error with its place in the file.
 */
export function checkFeedback(run: SetupCheckRun, intro: string): string {
  const lines = run.errors.map((error) => {
    const line = errorLine(run.text, error);
    const place = [error.section && `[${error.section}]`, error.key].filter(Boolean).join(" ");
    return `- optimize.toml${line ? `:${line}` : ""}${place ? ` ${place}` : ""}: ${error.message}`;
  });
  return [intro, ...lines].join("\n");
}
