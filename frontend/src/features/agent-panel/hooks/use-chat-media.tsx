"use client";

import * as React from "react";
import { toast } from "react-toastify";

import { msg } from "@/shared/lib/messages";
import { MAX_ATTACHMENT_MB } from "@/shared/ui/agent/file-attachments";
import {
  fitOriginals,
  keptOriginals,
  nameKey,
  originalFormat,
  pairedPlaces,
} from "@/shared/ui/agent/file-originals";
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
  fetchChatMedia,
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

/** The stored original of an attached file in the draft (SKY-735, SKY-782). It has no tile. */
interface DraftOriginal {
  localId: string;
  name: string;
  /** What the draft's viewer shows: the local file, by its object URL. One
   *  object for the original's life, so an upload that ends does not reload it. */
  shown: MediaAttachment;
  /** The stored file's id; null while the upload runs. */
  serverId: string | null;
}

export interface ChatMediaDraft {
  /** The tiles to show in the composer, keyed by local id. */
  tiles: readonly MediaAttachment[];
  /** The originals that go with ``files`` on Send, keyed by local id, which the draft's viewer shows. */
  shownOriginals: (files: ReadonlyArray<{ name: string }>) => readonly MediaAttachment[];
  /** Upload picked, pasted or dropped media files. */
  addFiles: (files: File[]) => void;
  /** Keep the originals of attached files; the viewer draws a PDF, DOCX or XLSX one. */
  addOriginals: (files: File[]) => void;
  /** Drop the originals whose files left the draft; ``files`` are the draft's files. */
  keepOriginals: (files: ReadonlyArray<{ name: string }>) => void;
  /**
   * Drop the originals that pair with the draft's ``files`` at the ``leaving``
   * indexes, also ones a restore still loads, so the other files keep theirs.
   */
  dropPaired: (files: ReadonlyArray<{ name: string }>, leaving: ReadonlySet<number>) => void;
  /** Take a tile out of the draft, with Undo; the stored file goes when the toast closes. */
  remove: (localId: string) => void;
  /** The files to send, by stored id, originals last; null while an upload still runs. */
  ready: () => MediaAttachment[] | null;
  /** Why Send must wait: an upload runs or failed. Null when the files can go. */
  blocked: string | null;
  /** Empty the draft after a send. The sent files stay stored; their URLs go on a chat switch. */
  clear: () => void;
  /** Upload a sent message's files again into an empty draft, as a fork puts back its text. */
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

/**
 * Free the queued local URLs that a committed draft no longer shows, and give
 * back the rest. React can run an older commit's effects after a URL is queued,
 * and that commit still shows the URL.
 */
function freeUnshown(
  queued: readonly string[],
  shown: ReadonlyArray<{ shown: { url: string } }>,
): string[] {
  const held = new Set(shown.map((o) => o.shown.url));
  for (const url of queued) if (!held.has(url)) freeMediaUrl(url);
  return queued.filter((url) => held.has(url));
}

/** The image, video and audio files in the composer draft (SKY-707). */
export function useChatMedia(): ChatMediaDraft {
  const [items, setItems] = React.useState<readonly DraftMedia[]>([]);
  // The handlers read the latest list between renders, as uploads end out of order.
  const itemsRef = React.useRef<readonly DraftMedia[]>([]);
  // Tiles taken out whose Undo toast is still open.
  const removedRef = React.useRef(new Map<string, DraftMedia>());
  const originalsRef = React.useRef<DraftOriginal[]>([]);
  const [draftOriginals, setDraftOriginals] = React.useState<readonly DraftOriginal[]>([]);
  const syncOriginals = React.useCallback((next: DraftOriginal[]) => {
    originalsRef.current = next;
    setDraftOriginals(next);
  }, []);
  // Local URLs of originals that left the draft. The viewer can still show one
  // until the draft without it commits, so this effect frees them after that.
  const unshownRef = React.useRef<string[]>([]);
  React.useEffect(() => {
    unshownRef.current = freeUnshown(unshownRef.current, draftOriginals);
  }, [draftOriginals]);
  // Restores whose files still load; Send waits for them.
  const [restoring, setRestoring] = React.useState(0);
  // For each name key: the places, among the key's documents, of the originals
  // the running restore still loads and still keeps.
  const loadingRef = React.useRef<Map<string, number[]>>(new Map());
  // A newer restore or a closed panel makes an older restore drop its files.
  const restoreRef = React.useRef(0);
  // The composer reads a file before it adds the original, and a toast outlives the
  // panel, so a call can come after the unmount cleanup that frees the draft.
  const closedRef = React.useRef(false);

  const update = React.useCallback(
    (next: (prev: readonly DraftMedia[]) => readonly DraftMedia[]) => {
      itemsRef.current = next(itemsRef.current);
      setItems(itemsRef.current);
    },
    [],
  );

  const upload = React.useCallback(
    (item: DraftMedia, file: File) => {
      uploadChatMedia(file)
        .then((stored) => {
          // The server takes a PDF or Office file from any upload as a document,
          // which the agent never sees. A media tile holding one has no file to send.
          if (stored.kind !== "document") return stored;
          void deleteChatMedia(stored.id);
          throw new ChatMediaError(415);
        })
        .then(
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
            if (closedRef.current) return;
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

  const keepFiles = React.useCallback(
    (files: File[], first: boolean) => {
      if (closedRef.current) return;
      // The server takes no original over the file limit, so a larger file
      // keeps only its text and is not uploaded for nothing.
      const fits = files.filter((file) => file.size <= MAX_ATTACHMENT_MB * 1024 * 1024);
      const added = fits.map((file): DraftOriginal => {
        const localId = crypto.randomUUID();
        // The viewer shows a PDF in an unsandboxed frame before the server checks
        // the bytes, so the blob's type is PDF whatever type the file claims.
        const blob =
          originalFormat(file.name) === "pdf"
            ? new Blob([file], { type: "application/pdf" })
            : file;
        return {
          localId,
          name: file.name,
          shown: { id: localId, name: file.name, kind: "document", url: localMediaUrl(blob) },
          serverId: null,
        };
      });
      // A restore's cards lead the draft, so its originals go first and pair with them.
      syncOriginals(
        first ? [...added, ...originalsRef.current] : [...originalsRef.current, ...added],
      );
      added.forEach((item, i) => {
        // A failed upload is silent: the file still goes as text, and its slide shows the text.
        const drop = () => {
          if (!originalsRef.current.some((o) => o.localId === item.localId)) return;
          unshownRef.current.push(item.shown.url);
          syncOriginals(originalsRef.current.filter((o) => o.localId !== item.localId));
        };
        uploadChatMedia(fits[i]!, { original: true }).then((stored) => {
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
  const addOriginals = React.useCallback((files: File[]) => keepFiles(files, false), [keepFiles]);

  const keepOriginals = React.useCallback(
    (files: ReadonlyArray<{ name: string }>) => {
      // An original a restore still loads counts as one the key holds. Else a
      // same-name file added meanwhile drops both originals.
      const loading = [...loadingRef.current].flatMap(([key, places]) =>
        places.map(() => ({ name: key })),
      );
      const kept = new Set<{ name: string }>(
        keptOriginals(files, [...loading, ...originalsRef.current]),
      );
      for (const key of loadingRef.current.keys()) {
        if (!kept.has(loading.find((l) => l.name === key)!)) loadingRef.current.delete(key);
      }
      const gone = originalsRef.current.filter((o) => !kept.has(o));
      if (!gone.length) return;
      syncOriginals(originalsRef.current.filter((o) => kept.has(o)));
      for (const o of gone) {
        unshownRef.current.push(o.shown.url);
        if (o.serverId) void deleteChatMedia(o.serverId);
      }
    },
    [syncOriginals],
  );

  const dropPaired = React.useCallback(
    (files: ReadonlyArray<{ name: string }>, leaving: ReadonlySet<number>) => {
      // A failed upload leaves its key with fewer originals than files, so the
      // places would not line up: such a key drops all its originals first.
      keepOriginals(files);
      for (const [key, places] of pairedPlaces(files, leaving)) {
        // The restore's originals that still load come first in the key's pairs.
        const loading = loadingRef.current.get(key) ?? [];
        const loaded = originalsRef.current.filter((o) => nameKey(o.name) === key);
        const late = new Set(places.filter((p) => p < loading.length).map((p) => loading[p]));
        if (late.size) {
          loadingRef.current.set(
            key,
            loading.filter((p) => !late.has(p)),
          );
        }
        const gone = places.flatMap((p) => loaded[p - loading.length] ?? []);
        if (!gone.length) continue;
        syncOriginals(originalsRef.current.filter((o) => !gone.includes(o)));
        for (const o of gone) {
          unshownRef.current.push(o.shown.url);
          if (o.serverId) void deleteChatMedia(o.serverId);
        }
      }
    },
    [keepOriginals, syncOriginals],
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
            } else if (undone && !closedRef.current) {
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
    const originals = fitOriginals(originalsRef.current, MAX_MEDIA_PER_MESSAGE - media.length).map(
      (o) => ({ id: o.serverId!, name: o.name, kind: "document" as const, url: "" }),
    );
    return [...media, ...originals];
  }, []);

  const clear = React.useCallback(() => {
    // The originals past the limit did not go with the message, so they go now.
    const sent = new Set(
      fitOriginals(originalsRef.current, MAX_MEDIA_PER_MESSAGE - itemsRef.current.length),
    );
    markMediaSent(itemsRef.current.map((m) => m.url));
    update(() => []);
    const gone = originalsRef.current;
    syncOriginals([]);
    // A sent original shows from its stored copy, so no local URL outlives the draft.
    for (const o of gone) {
      unshownRef.current.push(o.shown.url);
      if (!sent.has(o) && o.serverId) void deleteChatMedia(o.serverId);
    }
  }, [update, syncOriginals]);

  const restore = React.useCallback(
    (media: readonly MediaAttachment[]) => {
      if (!media.length || itemsRef.current.length) return;
      const run = ++restoreRef.current;
      // Each document's place among the restore's documents with its key.
      loadingRef.current = new Map();
      const places = media.map((m) => {
        if (m.kind !== "document") return -1;
        const key = nameKey(m.name);
        const loading = loadingRef.current.get(key) ?? [];
        loadingRef.current.set(key, [...loading, loading.length]);
        return loading.length;
      });
      setRestoring((n) => n + 1);
      // Each file uploads again as a new draft file. A sent id is the source
      // chat's: the server stores a copy under a new id that the client never learns.
      void Promise.allSettled(media.map((m) => fetchChatMedia(m.id))).then((blobs) => {
        setRestoring((n) => n - 1);
        // The user added files while these loaded, or the panel closed: the draft is theirs now.
        if (run !== restoreRef.current) return;
        const kept = loadingRef.current;
        loadingRef.current = new Map();
        if (itemsRef.current.length) return;
        // A document is a file's stored original (SKY-735), not a tile. One that
        // does not load stays out: its file still goes as text.
        const originals = media.flatMap((m, i) => {
          const got = blobs[i]!;
          // A card that left while its original loaded takes the original with it.
          if (!kept.get(nameKey(m.name))?.includes(places[i]!)) return [];
          return m.kind === "document" && got.status === "fulfilled"
            ? [new File([got.value], m.name, { type: got.value.type })]
            : [];
        });
        const added = media.flatMap((m, i): Array<{ item: DraftMedia; file: File | null }> => {
          const got = blobs[i]!;
          if (m.kind === "document") return [];
          if (got.status === "rejected") {
            toast.error(msg("agent.media.upload_failed", { name: m.name }));
            // The tile stays, marked, so the user sees which file is missing.
            const item: DraftMedia = {
              localId: crypto.randomUUID(),
              name: m.name,
              kind: m.kind,
              url: "",
              serverId: null,
              failed: true,
            };
            return [{ item, file: null }];
          }
          const file = new File([got.value], m.name, { type: got.value.type });
          const item: DraftMedia = {
            localId: crypto.randomUUID(),
            name: m.name,
            kind: m.kind,
            url: localMediaUrl(file),
            serverId: null,
            failed: false,
          };
          return [{ item, file }];
        });
        update(() => added.map((a) => a.item));
        for (const { item, file } of added) if (file) upload(item, file);
        if (originals.length) keepFiles(originals, true);
      });
    },
    [update, upload, keepFiles],
  );

  // A closed panel frees the local URLs of files still in the draft. An upload
  // that ends later then finds no tile or original and deletes its stored file.
  React.useEffect(() => {
    // StrictMode unmounts and mounts again, so the mount opens the draft again.
    closedRef.current = false;
    return () => {
      closedRef.current = true;
      restoreRef.current += 1;
      for (const m of itemsRef.current) freeMediaUrl(m.url);
      for (const o of originalsRef.current) freeMediaUrl(o.shown.url);
      for (const url of unshownRef.current.splice(0)) freeMediaUrl(url);
      itemsRef.current = [];
      originalsRef.current = [];
    };
  }, []);

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
  // The same originals that ``ready`` sends after Send's ``keepOriginals``, so a
  // tile never shows an original that stays out of the message.
  const shownOriginals = React.useCallback(
    (files: ReadonlyArray<{ name: string }>) =>
      fitOriginals(keptOriginals(files, draftOriginals), MAX_MEDIA_PER_MESSAGE - items.length).map(
        (o) => o.shown,
      ),
    [draftOriginals, items.length],
  );
  const originalsUploading = draftOriginals.some((o) => !o.serverId);
  const blocked = items.some((m) => m.failed)
    ? msg("agent.media.remove_failed")
    : restoring || items.some((m) => !m.serverId) || originalsUploading
      ? msg("agent.media.uploading")
      : null;

  return {
    tiles,
    shownOriginals,
    addFiles,
    addOriginals,
    keepOriginals,
    dropPaired,
    remove,
    ready,
    blocked,
    clear,
    restore,
  };
}
