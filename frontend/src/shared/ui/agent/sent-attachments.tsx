"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

import { pairOriginals } from "./file-originals";
import { FileTile, fileTilePreview } from "./file-tiles";
import {
  DraftTile,
  FailedTile,
  type MediaAttachment,
  MediaViewer,
  PendingTile,
  useResolvedMedia,
  type ViewerItem,
} from "./media-attachments";
import type { AttachedFile } from "./pasted-text";

/** The most tiles a sent message shows; the last one then carries "+N" for the rest. */
export const SENT_LIMIT = 4;

const NO_MEDIA: readonly MediaAttachment[] = [];

/** One tile of a sent message, and its slide in the viewer (null when it has none). */
interface Entry {
  key: string;
  slide: number | null;
  media?: MediaAttachment;
  /** The media's URL is still on its way. */
  pending?: boolean;
  file?: AttachedFile;
}

/**
 * The images, video, audio and files sent with a message, as the composer's
 * tiles, media first. Past four, the fourth tile carries a dimmed "+N" for the
 * rest, as WhatsApp does. Every tile opens one shared viewer at its own item;
 * the viewer pages through all of them. A file with nothing to show and media
 * that did not upload open nothing.
 */
export function SentAttachments({
  media: given = NO_MEDIA,
  files,
}: {
  media?: readonly MediaAttachment[];
  files: readonly AttachedFile[];
}) {
  // An original shows only in its file's slide, so it is not fetched until that slide opens.
  const { shown: tiled, originals } = React.useMemo(
    () => ({
      shown: given.filter((m) => m.kind !== "document"),
      originals: given.filter((m) => m.kind === "document"),
    }),
    [given],
  );
  const resolved = useResolvedMedia(tiled);
  // The open item's id: an index would move to another item as late URLs add slides.
  const [open, setOpen] = React.useState<string | null>(null);
  const opener = React.useRef<HTMLElement | null>(null);
  const { slides, entries } = React.useMemo(() => {
    const slides: ViewerItem[] = [];
    const entries: Entry[] = [];
    const ready = new Map(resolved.map((m) => [m.id, m]));
    for (const g of tiled) {
      const m = ready.get(g.id);
      if (!m) {
        // A placeholder keeps the tile's place, so nothing moves when the URL arrives.
        entries.push({ key: g.id, slide: null, media: g, pending: true });
        continue;
      }
      if (m.status === "failed") {
        entries.push({ key: m.id, slide: null, media: m });
        continue;
      }
      entries.push({
        key: m.id,
        slide: slides.push(m) - 1,
        media: m,
      });
    }
    const paired = pairOriginals(files, originals);
    files.forEach((file, i) => {
      const preview = fileTilePreview(file.name, file.text);
      const original = paired[i] ?? undefined;
      const id = `file-${i}-${file.name}`;
      entries.push({
        key: id,
        slide:
          preview || original
            ? slides.push({ kind: "file", id, name: file.name, preview, original }) - 1
            : null,
        file,
      });
    });
    return { slides, entries };
  }, [tiled, resolved, files, originals]);
  if (!entries.length) return null;
  const hidden = entries.length > SENT_LIMIT ? entries.length - SENT_LIMIT : 0;
  const shown = hidden ? entries.slice(0, SENT_LIMIT) : entries;
  // "+N" opens the viewer at its own tile, or at the first hidden item that has a slide.
  const moreSlide = hidden
    ? (entries.slice(SENT_LIMIT - 1).find((e) => e.slide !== null)?.slide ?? null)
    : null;
  const openAt = (slide: number, e: React.MouseEvent<HTMLElement>) => {
    opener.current = e.currentTarget;
    setOpen(slides[slide]?.id ?? null);
  };
  const openIndex = open === null ? -1 : slides.findIndex((s) => s.id === open);
  return (
    <>
      <ul
        role="list"
        aria-label={msg("shared.agent.media_list")}
        className="flex max-w-full flex-wrap items-end justify-end gap-2"
      >
        {shown.map(({ key, slide, media: m, pending, file }, i) => {
          const covered = hidden > 0 && i === SENT_LIMIT - 1;
          return (
            <li key={key} className="relative">
              {/* Under "+N" the tile is only a picture; the "+N" button takes its place. */}
              <div inert={covered}>
                {file ? (
                  <FileTile
                    name={file.name}
                    text={file.text}
                    onOpen={slide === null ? undefined : (e) => openAt(slide, e)}
                  />
                ) : pending && m ? (
                  <PendingTile media={m} />
                ) : m?.status === "failed" ? (
                  <FailedTile media={m} />
                ) : (
                  m && slide !== null && <DraftTile media={m} onOpen={(e) => openAt(slide, e)} />
                )}
              </div>
              {covered && (
                <button
                  type="button"
                  onClick={(e) => {
                    if (moreSlide !== null) openAt(moreSlide, e);
                  }}
                  disabled={moreSlide === null}
                  aria-label={msg("shared.agent.media_more", { count: hidden })}
                  className={cn(
                    "absolute inset-0 z-[3] flex items-center justify-center rounded-xl bg-black/55 text-2xl font-medium text-white tabular-nums",
                    "transition-colors duration-150 hover:bg-black/65 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
                    moreSlide === null ? "cursor-default" : "cursor-pointer",
                  )}
                >
                  +{hidden}
                </button>
              )}
            </li>
          );
        })}
      </ul>
      <MediaViewer
        media={slides}
        index={openIndex < 0 ? null : openIndex}
        onIndex={(i) => setOpen(slides[i]?.id ?? null)}
        onClose={() => setOpen(null)}
        returnFocus={opener}
      />
    </>
  );
}
