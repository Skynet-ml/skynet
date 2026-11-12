/**
 * A run setup the user left. The server keeps the account's one draft until
 * the user submits or picks Start new; this browser keeps a copy only while
 * the server has not taken it. It holds the raw text of the tab's setup keys
 * (answers, screen, schedule, the setup chat), so Continue only writes them
 * back and the wizard reopens as after a reload.
 */
export const SETUP_DRAFT_KEY = "skynet.wizard.draft.v1";

/** The agent submitted the setup; the left draft must not come back. */
export const SETUP_SUBMITTED_EVENT = "skynet:setup-submitted";

/** The tab's copy of the setup chat changed, so the draft should save. */
export const SETUP_CHAT_KEPT_EVENT = "skynet:setup-chat-kept";

export interface SetupDraft {
  account: string;
  /** When the user last changed or left the setup, in epoch ms; for display only. */
  leftAt: number;
  values: Record<string, string>;
  /** On a browser copy: the page and count of the save that sent it, so a load can tell if it landed. */
  save?: { writer: string; seq: number };
  /** On a browser copy: the server slot revision it builds on. */
  baseRevision?: number;
}

/** Serialize a left setup for local storage. */
export function encodeSetupDraft(draft: SetupDraft): string {
  return JSON.stringify(draft);
}

/**
 * Read a left setup back; null when it is missing or malformed.
 *
 * Args:
 *   raw: The stored text, or null.
 *
 * Returns:
 *   The draft, or null.
 */
export function decodeSetupDraft(raw: string | null): SetupDraft | null {
  if (!raw) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object") return null;
  const { account, leftAt, values, save, baseRevision } = parsed as Record<string, unknown>;
  if (typeof account !== "string" || !account) return null;
  if (typeof leftAt !== "number" || !Number.isFinite(leftAt)) return null;
  if (!values || typeof values !== "object" || Array.isArray(values)) return null;
  const kept: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    if (typeof value === "string") kept[key] = value;
  }
  const draft: SetupDraft = { account, leftAt, values: kept };
  const mark = save as { writer?: unknown; seq?: unknown } | null | undefined;
  if (typeof mark?.writer === "string" && Number.isInteger(mark.seq)) {
    draft.save = { writer: mark.writer, seq: mark.seq as number };
  }
  if (Number.isInteger(baseRevision)) draft.baseRevision = baseRevision as number;
  return draft;
}

/** The stored text of the left setup, or null when storage is blocked. */
export function readSetupDraft(): string | null {
  try {
    return window.localStorage.getItem(SETUP_DRAFT_KEY);
  } catch {
    return null;
  }
}

/** Keep a left setup; false when full or blocked storage dropped it. */
export function writeSetupDraft(draft: SetupDraft): boolean {
  try {
    window.localStorage.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft));
    return true;
  } catch {
    return false;
  }
}

// Browsers refuse keepalive bodies over 64 KiB in total, counted in bytes; the
// margin leaves room for another keepalive call on the same page hide.
const KEEPALIVE_MAX_BYTES = 48 * 1024;

/** Whether a draft body may go as a keepalive call, which outlives the page. */
export function fitsKeepalive(body: string): boolean {
  return new TextEncoder().encode(body).length <= KEEPALIVE_MAX_BYTES;
}

export function clearSetupDraft(): void {
  try {
    window.localStorage.removeItem(SETUP_DRAFT_KEY);
  } catch {}
}
