"use client";

import * as React from "react";
import { toast } from "react-toastify";

import { msg } from "@/shared/lib/messages";
import {
  mediaKind,
  type MediaAttachment,
  type MediaKind,
} from "@/shared/ui/agent/media-attachments";
import {
  ChatMediaError,
  MAX_MEDIA_BYTES,
  MAX_MEDIA_PER_MESSAGE,
  deleteChatMedia,
  fetchOwnMediaUrl,
  freeMediaUrl,
  localMediaUrl,
  markMediaSent,
  uploadChatMedia,
} from "../lib/chat-media";

/** A file in the composer: it plays from its local URL and goes out by its stored id. */
interface DraftMedia {
  localId: string;
  name: string;
  kind: MediaKind;
  url: string;
  /** The stored file's id; null while the upload runs. */
  serverId: string | null;
  /** The upload failed; the tile stays, marked, until the user removes it. */
  failed: boolean;
  /** A file of a sent message, put back by a fork. Another chat holds it, so a remove keeps it. */
  restored?: boolean;
}

export interface ChatMediaDraft {
  /** The tiles to show in the composer, keyed by local id. */
  tiles: readonly MediaAttachment[];
  /** Upload picked, pasted or dropped media files. */
  addFiles: (files: File[]) => void;
  /** Take a tile out of the draft, with Undo; the stored file goes when the toast closes. */
  remove: (localId: string) => void;
  /** The files to send, by stored id; null while an upload still runs. */
  ready: () => MediaAttachment[] | null;
  /** Why Send must wait: an upload runs or failed. Null when the files can go. */
  blocked: string | null;
  /** Empty the draft after a send. The sent files stay stored; their URLs go on a chat switch. */
  clear: () => void;
  /** Put a sent message's files back into an empty draft, as a fork does with its text. */
  restore: (media: readonly MediaAttachment[]) => void;
}

/** The toast for a failed upload, from the server's status. */
function uploadError(name: string, err: unknown): string {
  const status = err instanceof ChatMediaError ? err.status : 0;
  if (status === 415) return msg("agent.media.unsupported", { name });
  if (status === 413) return msg("agent.media.too_large", { name });
  if (status === 409) return msg("agent.media.quota");
  return msg("agent.media.upload_failed", { name });
}

/** The image, video and audio files in the composer draft (SKY-707). */
export function useChatMedia(): ChatMediaDraft {
  const [items, setItems] = React.useState<readonly DraftMedia[]>([]);
  // The handlers read the latest list between renders, as uploads end out of order.
  const itemsRef = React.useRef<readonly DraftMedia[]>([]);
  // Tiles taken out whose Undo toast is still open.
  const removedRef = React.useRef(new Map<string, DraftMedia>());

  const update = React.useCallback(
    (next: (prev: readonly DraftMedia[]) => readonly DraftMedia[]) => {
      itemsRef.current = next(itemsRef.current);
      setItems(itemsRef.current);
    },
    [],
  );

  const upload = React.useCallback(
    (item: DraftMedia, file: File) => {
      uploadChatMedia(file).then(
        (stored) => {
          const removed = removedRef.current.get(item.localId);
          if (itemsRef.current.some((m) => m.localId === item.localId)) {
            update((prev) =>
              prev.map((m) => (m.localId === item.localId ? { ...m, serverId: stored.id } : m)),
            );
          } else if (removed) {
            removed.serverId = stored.id;
          } else {
            // The tile was removed for good while it uploaded.
            void deleteChatMedia(stored.id);
          }
        },
        (err: unknown) => {
          // The tile stays with a failed mark, so the user sees which file did not go.
          const removed = removedRef.current.get(item.localId);
          if (removed) removed.failed = true;
          update((prev) =>
            prev.map((m) => (m.localId === item.localId ? { ...m, failed: true } : m)),
          );
          toast.error(uploadError(item.name, err));
        },
      );
    },
    [update],
  );

  const addFiles = React.useCallback(
    (files: File[]) => {
      const room = MAX_MEDIA_PER_MESSAGE - itemsRef.current.length;
      if (files.length > room)
        toast.error(msg("agent.media.too_many", { max: MAX_MEDIA_PER_MESSAGE }));
      const added: DraftMedia[] = [];
      for (const file of files.slice(0, Math.max(0, room))) {
        const kind = mediaKind(file);
        if (!kind) continue;
        const name = file.name || kind;
        if (file.size > MAX_MEDIA_BYTES[kind]) {
          toast.error(msg("agent.media.too_large", { name }));
          continue;
        }
        const item: DraftMedia = {
          localId: crypto.randomUUID(),
          name,
          kind,
          url: localMediaUrl(file),
          serverId: null,
          failed: false,
        };
        added.push(item);
        upload(item, file);
      }
      if (added.length) update((prev) => [...prev, ...added]);
    },
    [update, upload],
  );

  const remove = React.useCallback(
    (localId: string) => {
      const index = itemsRef.current.findIndex((m) => m.localId === localId);
      const item = itemsRef.current[index];
      if (!item) return;
      update((prev) => prev.filter((m) => m.localId !== localId));
      removedRef.current.set(localId, item);
      let undone = false;
      toast(
        ({ closeToast }) => (
          <span className="flex items-center gap-3">
            <span className="min-w-0 flex-1 truncate">
              {msg("agent.media.removed", { name: item.name })}
            </span>
            <button
              type="button"
              onClick={() => {
                undone = true;
                closeToast();
              }}
              className="shrink-0 cursor-pointer font-semibold text-primary hover:underline focus-visible:underline focus-visible:outline-none"
            >
              {msg("agent.media.undo")}
            </button>
          </span>
        ),
        {
          autoClose: 5000,
          onClose: () => {
            const held = removedRef.current.get(localId);
            removedRef.current.delete(localId);
            if (!held) return;
            // Files added while the toast was open can fill the message; then
            // the Undo fails and the file goes, so a message never passes the limit.
            if (undone && itemsRef.current.length >= MAX_MEDIA_PER_MESSAGE) {
              toast.error(msg("agent.media.too_many", { max: MAX_MEDIA_PER_MESSAGE }));
            } else if (undone) {
              update((prev) => {
                const next = prev.slice();
                next.splice(Math.min(index, next.length), 0, held);
                return next;
              });
              return;
            }
            if (held.serverId && !held.restored) void deleteChatMedia(held.serverId);
            freeMediaUrl(held.url);
          },
        },
      );
    },
    [update],
  );

  const ready = React.useCallback((): MediaAttachment[] | null => {
    const current = itemsRef.current;
    if (current.some((m) => !m.serverId)) return null;
    return current.map((m) => ({ id: m.serverId!, name: m.name, kind: m.kind, url: m.url }));
  }, []);

  const clear = React.useCallback(() => {
    markMediaSent(itemsRef.current.map((m) => m.url));
    update(() => []);
  }, [update]);

  const restore = React.useCallback(
    (media: readonly MediaAttachment[]) => {
      if (!media.length || itemsRef.current.length) return;
      // The sent ids go out again; the server copies each file into the chat that gets it.
      void Promise.allSettled(media.map((m) => fetchOwnMediaUrl(m.id))).then((urls) => {
        const restored = media.map((m, i): DraftMedia => {
          const got = urls[i]!;
          return {
            localId: crypto.randomUUID(),
            name: m.name,
            kind: m.kind,
            url: got.status === "fulfilled" ? got.value : "",
            serverId: m.id,
            // A file that did not load shows as failed, so the user sees what is missing.
            failed: got.status === "rejected",
            restored: true,
          };
        });
        // The user added files while these loaded: the draft is theirs now.
        if (itemsRef.current.length) {
          for (const m of restored) freeMediaUrl(m.url);
          return;
        }
        update(() => restored);
      });
    },
    [update],
  );

  // A closed panel frees the local URLs of files still in the draft. An upload
  // that ends later then finds no tile and deletes its stored file.
  React.useEffect(
    () => () => {
      for (const m of itemsRef.current) freeMediaUrl(m.url);
      itemsRef.current = [];
    },
    [],
  );

  const tiles = React.useMemo(
    () =>
      items.map((m) => ({
        id: m.localId,
        name: m.name,
        kind: m.kind,
        url: m.url,
        ...(m.failed ? { status: "failed" as const } : {}),
      })),
    [items],
  );
  const blocked = items.some((m) => m.failed)
    ? msg("agent.media.remove_failed")
    : items.some((m) => !m.serverId)
      ? msg("agent.media.uploading")
      : null;

  return { tiles, addFiles, remove, ready, blocked, clear, restore };
}
