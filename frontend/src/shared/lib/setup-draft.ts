/**
 * A run setup the user left, kept in this browser for a short time so a
 * return to /submit can offer it back. It holds the raw text of the tab's
 * setup keys (answers, screen, schedule, the setup chat), so Continue only
 * writes them back and the wizard reopens as after a reload.
 */
export const SETUP_DRAFT_KEY = "skynet.wizard.draft.v1";
export const SETUP_DRAFT_TTL_MS = 10 * 60 * 1000;

export interface SetupDraft {
  account: string;
  /** When the user left the setup, in epoch ms; the time limit counts from here. */
  leftAt: number;
  values: Record<string, string>;
}

/** Serialize a left setup for local storage. */
export function encodeSetupDraft(draft: SetupDraft): string {
  return JSON.stringify(draft);
}

/**
 * Read a left setup back; null when it is missing, malformed or older than
 * the time limit.
 *
 * Args:
 *   raw: The stored text, or null.
 *   now: The current time, in epoch ms; null reads an expired draft too.
 *
 * Returns:
 *   The draft, or null.
 */
export function decodeSetupDraft(raw: string | null, now: number | null): SetupDraft | null {
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const { account, leftAt, values } = parsed as Record<string, unknown>;
  if (typeof account !== "string" || !account) return null;
  if (typeof leftAt !== "number" || !Number.isFinite(leftAt)) return null;
  if (now !== null && (now - leftAt > SETUP_DRAFT_TTL_MS || leftAt - now > 60_000)) return null;
  if (!values || typeof values !== "object" || Array.isArray(values)) return null;
  const kept: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    if (typeof value === "string") kept[key] = value;
  }
  return { account, leftAt, values: kept };
}

/** The stored text of the left setup, or null when storage is blocked. */
export function readSetupDraft(): string | null {
  try {
    return window.localStorage.getItem(SETUP_DRAFT_KEY);
  } catch {
    return null;
  }
}

/** Keep a left setup; full or blocked storage just drops it. */
export function writeSetupDraft(draft: SetupDraft): void {
  try {
    window.localStorage.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft));
  } catch {}
}

export function clearSetupDraft(): void {
  try {
    window.localStorage.removeItem(SETUP_DRAFT_KEY);
  } catch {}
}
