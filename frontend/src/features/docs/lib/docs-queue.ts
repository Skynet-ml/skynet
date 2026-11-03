export type DocsAskNotice = "rate" | "tooLong" | "error" | null;

/** Release an unread response that arrived after its question was stopped or replaced. */
export async function discardStaleDocsResponse(response: Response, stale: boolean): Promise<boolean> {
  if (!stale) return false;
  await response.body?.cancel().catch(() => undefined);
  return true;
}

/** Classify the ask response and hold follow-ups after a rate limit. */
export function docsAskFailure(status: number, hasBody: boolean, pause: () => void): DocsAskNotice {
  if (status === 429) {
    pause();
    return "rate";
  }
  if (status === 422) return "tooLong";
  if (status < 200 || status >= 300 || !hasBody) return "error";
  return null;
}
