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
  freeMediaUrl,
  localMediaUrl,
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
  /** Empty the draft after a send. The sent files stay stored and their URLs stay live. */
  clear: () => void;
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
          update((prev) => prev.filter((m) => m.localId !== item.localId));
          removedRef.current.delete(item.localId);
          freeMediaUrl(item.url);
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
            if (held.serverId) void deleteChatMedia(held.serverId);
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

  const clear = React.useCallback(() => update(() => []), [update]);

  const tiles = React.useMemo(
    () => items.map((m) => ({ id: m.localId, name: m.name, kind: m.kind, url: m.url })),
    [items],
  );

  return { tiles, addFiles, remove, ready, clear };
}
