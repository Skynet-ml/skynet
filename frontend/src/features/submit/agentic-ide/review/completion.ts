// The editor's call to POST /agent-ide/completions (SKY-703). Each call is a
// paid model call, so a refusal (no funds, no key, over the rate limit) pauses
// the calls for a while instead of asking again after every key.

import { apiUrl, fetchWithAuthRetry } from "@/shared/lib/api";

import type { Suggestion } from "./suggestions";

const PAUSE_MS = 60_000;
const PAUSE_STATUSES = new Set([402, 403, 409, 429, 503]);
let pausedUntil = 0;

/** Ask the model for the completion at the caret; null on any failure or while paused. */
export async function fetchCompletion(
  req: { path: string; prefix: string; suffix: string },
  signal: AbortSignal,
): Promise<Suggestion | null> {
  if (Date.now() < pausedUntil) return null;
  try {
    const res = await fetchWithAuthRetry(apiUrl("/agent-ide/completions"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
      signal,
    });
    if (!res.ok) {
      if (PAUSE_STATUSES.has(res.status)) pausedUntil = Date.now() + PAUSE_MS;
      return null;
    }
    const data = (await res.json()) as { completion?: string; next_edit?: Suggestion["next"] };
    return { text: data.completion ?? "", next: data.next_edit ?? null };
  } catch {
    // An abort or a network drop: a completion is optional help, so show nothing.
    return null;
  }
}
