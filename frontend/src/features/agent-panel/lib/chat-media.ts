// Client for the chat media routes (SKY-707): upload, delete, and read back
// the images, video and audio a user attaches in the setup chat.
//
// Stored files need the bearer token, so an <img src> cannot load them
// directly. The page fetches each file once as a blob and plays it from an
// object URL.

import { apiUrl, fetchWithAuthRetry } from "@/shared/lib/api";
import type { MediaAttachment, MediaKind } from "@/shared/ui/agent/media-attachments";

/** The upload limit for each kind; the server checks the same numbers. */
export const MAX_MEDIA_BYTES: Record<MediaKind, number> = {
  image: 3 * 1024 * 1024,
  audio: 20 * 1024 * 1024,
  video: 50 * 1024 * 1024,
};

/** The most files one message can carry; the server checks the same number. */
export const MAX_MEDIA_PER_MESSAGE = 10;

export interface StoredMedia {
  id: string;
  name: string;
  kind: MediaAttachment["kind"];
}

/** A failed media request; ``status`` is the HTTP status, 0 for a network failure. */
export class ChatMediaError extends Error {
  constructor(readonly status: number) {
    super(`chat media request failed (${status})`);
  }
}

/** Upload one file; the server finds its real type from its bytes. */
export async function uploadChatMedia(
  file: File,
  { original = false }: { original?: boolean } = {},
): Promise<StoredMedia> {
  const body = new FormData();
  body.append("file", file, file.name);
  // An attached file's original is stored whatever its type (SKY-782).
  if (original) body.append("original", "true");
  const res = await fetchWithAuthRetry(apiUrl("/agent/media"), { method: "POST", body }).catch(
    () => null,
  );
  if (!res?.ok) throw new ChatMediaError(res?.status ?? 0);
  const data = (await res.json()) as StoredMedia;
  return { id: data.id, name: data.name, kind: data.kind };
}

/** Delete one stored file. A failure is silent: the server drops old files on its own. */
export async function deleteChatMedia(id: string): Promise<void> {
  await fetchWithAuthRetry(apiUrl(`/agent/media/${encodeURIComponent(id)}`), {
    method: "DELETE",
  }).catch(() => null);
}

// Object URLs this page made and has not freed. A URL outside this set is
// from an earlier page load (a restored setup chat), so it is dead.
const liveUrls = new Set<string>();
const fetched = new Map<string, Promise<string>>();
// Local URLs of sent files. The sent turn plays from them until the panel shows another chat.
const sentUrls = new Set<string>();

/** Make an object URL for a local file and track it as live. */
export function localMediaUrl(file: Blob): string {
  const url = URL.createObjectURL(file);
  liveUrls.add(url);
  return url;
}

/** Keep the local URLs of sent files until the panel shows another chat. */
export function markMediaSent(urls: readonly string[]): void {
  for (const url of urls) if (liveUrls.has(url)) sentUrls.add(url);
}

/** Free an object URL from ``localMediaUrl``. */
export function freeMediaUrl(url: string): void {
  if (!liveUrls.delete(url)) return;
  URL.revokeObjectURL(url);
}

/** A URL that plays the attachment: its own when live, else the stored file fetched by id. */
export function resolveChatMediaUrl(media: MediaAttachment): Promise<string> {
  if (liveUrls.has(media.url)) return Promise.resolve(media.url);
  let pending = fetched.get(media.id);
  if (!pending) {
    pending = fetchWithAuthRetry(apiUrl(`/agent/media/${encodeURIComponent(media.id)}`), {}).then(
      async (res) => {
        if (!res.ok) throw new ChatMediaError(res.status);
        return localMediaUrl(await res.blob());
      },
    );
    // A failed fetch is not cached, so a later render can try again.
    pending.catch(() => fetched.delete(media.id));
    fetched.set(media.id, pending);
  }
  return pending;
}

/** Fetch a stored file's bytes, to upload them again as a new draft file. */
export async function fetchChatMedia(id: string): Promise<Blob> {
  const res = await fetchWithAuthRetry(apiUrl(`/agent/media/${encodeURIComponent(id)}`), {}).catch(
    () => null,
  );
  if (!res?.ok) throw new ChatMediaError(res?.status ?? 0);
  return res.blob();
}

/**
 * Free the object URLs of the files a chat shows: the stored files fetched for display and
 * the local URLs of sent files. The panel calls it when it shows another chat, so these URLs
 * never outlive one chat. A chat shown again fetches its files by id.
 */
export function freeChatMediaUrls(): void {
  // A fetch still in flight frees its URL when it lands.
  for (const pending of fetched.values()) pending.then(freeMediaUrl, () => {});
  fetched.clear();
  for (const url of sentUrls) freeMediaUrl(url);
  sentUrls.clear();
}
