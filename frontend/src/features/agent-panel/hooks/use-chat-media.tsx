"use client";

import * as React from "react";
import { toast } from "react-toastify";

import { msg } from "@/shared/lib/messages";
import { keptOriginals } from "@/shared/ui/agent/file-originals";
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
}

/** The stored original of a PDF, DOCX or XLSX in the draft (SKY-735). It has no tile. */
interface DraftOriginal {
  localId: string;
  name: string;
  /** The stored file's id; null while the upload runs. */
  serverId: string | null;
}

export interface ChatMediaDraft {
  /** The tiles to show in the composer, keyed by local id. */
  tiles: readonly MediaAttachment[];
  /** Upload picked, pasted or dropped media files. */
  addFiles: (files: File[]) => void;
  /** Keep the originals of attached PDF, DOCX and XLSX files, which the viewer shows. */
  addOriginals: (files: File[]) => void;
  /** Drop the originals whose files left the draft; ``files`` are the draft's files. */
  keepOriginals: (files: ReadonlyArray<{ name: string }>) => void;
  /** Take a tile out of the draft, with Undo; the stored file goes when the toast closes. */
  remove: (localId: string) => void;
  /** The files to send, by stored id, originals last; null while an upload still runs. */
  ready: () => MediaAttachment[] | null;
  /** Why Send must wait: an upload runs or failed. Null when the files can go. */
  blocked: string | null;
  /** Empty the draft after a send. The sent files stay stored; their URLs go on a chat switch. */
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
  const originalsRef = React.useRef<DraftOriginal[]>([]);
  const [originalsUploading, setOriginalsUploading] = React.useState(false);
  const syncOriginals = React.useCallback((next: DraftOriginal[]) => {
    originalsRef.current = next;
    setOriginalsUploading(next.some((o) => !o.serverId));
  }, []);

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

  const addOriginals = React.useCallback(
    (files: File[]) => {
      const added = files.map((file) => ({
        localId: crypto.randomUUID(),
        name: file.name,
        serverId: null,
      }));
      syncOriginals([...originalsRef.current, ...added]);
      added.forEach((item, i) => {
        // A failed upload is silent: the file still goes as text, and its slide shows the text.
        const drop = () =>
          syncOriginals(originalsRef.current.filter((o) => o.localId !== item.localId));
        uploadChatMedia(files[i]!).then((stored) => {
          if (!originalsRef.current.some((o) => o.localId === item.localId)) {
            void deleteChatMedia(stored.id);
            return;
          }
          if (stored.kind !== "document") {
            void deleteChatMedia(stored.id);
            drop();
            return;
          }
          syncOriginals(
            originalsRef.current.map((o) =>
              o.localId === item.localId ? { ...o, serverId: stored.id } : o,
            ),
          );
        }, drop);
      });
    },
    [syncOriginals],
  );

  const keepOriginals = React.useCallback(
    (files: ReadonlyArray<{ name: string }>) => {
      const kept = new Set(keptOriginals(files, originalsRef.current));
      if (kept.size === originalsRef.current.length) return;
      for (const o of originalsRef.current)
        if (!kept.has(o) && o.serverId) void deleteChatMedia(o.serverId);
      syncOriginals(originalsRef.current.filter((o) => kept.has(o)));
    },
    [syncOriginals],
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
    if (current.some((m) => !m.serverId) || originalsRef.current.some((o) => !o.serverId))
      return null;
    const media = current.map((m) => ({ id: m.serverId!, name: m.name, kind: m.kind, url: m.url }));
    // Past the message limit an original stays out; its file still goes as text.
    const originals = originalsRef.current
      .slice(0, Math.max(0, MAX_MEDIA_PER_MESSAGE - media.length))
      .map((o) => ({ id: o.serverId!, name: o.name, kind: "document" as const, url: "" }));
    return [...media, ...originals];
  }, []);

  const clear = React.useCallback(() => {
    // The originals past the limit did not go with the message, so they go now.
    const room = Math.max(0, MAX_MEDIA_PER_MESSAGE - itemsRef.current.length);
    markMediaSent(itemsRef.current.map((m) => m.url));
    update(() => []);
    for (const o of originalsRef.current.slice(room))
      if (o.serverId) void deleteChatMedia(o.serverId);
    syncOriginals([]);
  }, [update, syncOriginals]);

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
    : items.some((m) => !m.serverId) || originalsUploading
      ? msg("agent.media.uploading")
      : null;

  return { tiles, addFiles, addOriginals, keepOriginals, remove, ready, blocked, clear };
}
