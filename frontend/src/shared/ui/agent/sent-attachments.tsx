"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

import { FileTile, fileTilePreview } from "./file-tiles";
import {
  DraftTile,
  FailedTile,
  type MediaAttachment,
  MediaViewer,
  useResolvedMedia,
  type ViewerItem,
} from "./media-attachments";
import type { AttachedFile } from "./pasted-text";

/** The most tiles a sent message shows; the last one then carries "+N" for the rest. */
export const SENT_LIMIT = 4;

/** One tile of a sent message, and its slide in the viewer (null when it has none). */
interface Entry {
  key: string;
  slide: number | null;
  media?: MediaAttachment;
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
  media: given = [],
  files,
}: {
  media?: readonly MediaAttachment[];
  files: readonly AttachedFile[];
}) {
  const media = useResolvedMedia(given);
  const [open, setOpen] = React.useState<number | null>(null);
  const opener = React.useRef<HTMLElement | null>(null);
  const { slides, entries } = React.useMemo(() => {
    const slides: ViewerItem[] = [];
    const entries: Entry[] = [];
    for (const m of media) {
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
    files.forEach((file, i) => {
      const preview = fileTilePreview(file.name, file.text);
      const id = `file-${i}-${file.name}`;
      entries.push({
        key: id,
        slide: preview ? slides.push({ kind: "file", id, name: file.name, preview }) - 1 : null,
        file,
      });
    });
    return { slides, entries };
  }, [media, files]);
  if (!entries.length) return null;
  const hidden = entries.length > SENT_LIMIT ? entries.length - SENT_LIMIT : 0;
  const shown = hidden ? entries.slice(0, SENT_LIMIT) : entries;
  // "+N" opens the viewer at its own tile, or at the first hidden item that has a slide.
  const moreSlide = hidden
    ? (entries.slice(SENT_LIMIT - 1).find((e) => e.slide !== null)?.slide ?? null)
    : null;
  return (
    <>
      <ul
        role="list"
        aria-label={msg("shared.agent.media_list")}
        className="flex max-w-full flex-wrap items-end justify-end gap-2"
      >
        {shown.map(({ key, slide, media: m, file }, i) => (
          <li key={key} className="relative">
            {file ? (
              <FileTile
                name={file.name}
                text={file.text}
                onOpen={
                  slide === null
                    ? undefined
                    : (e) => {
                        opener.current = e.currentTarget;
                        setOpen(slide);
                      }
                }
              />
            ) : m?.status === "failed" ? (
              <FailedTile media={m} />
            ) : (
              m && (
                <DraftTile
                  media={m}
                  onOpen={(e) => {
                    opener.current = e.currentTarget;
                    setOpen(slide);
                  }}
                />
              )
            )}
            {hidden > 0 && i === SENT_LIMIT - 1 && (
              <button
                type="button"
                onClick={(e) => {
                  if (moreSlide === null) return;
                  opener.current = e.currentTarget;
                  setOpen(moreSlide);
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
        ))}
      </ul>
      <MediaViewer
        media={slides}
        index={open === null || !slides.length ? null : Math.min(open, slides.length - 1)}
        onIndex={setOpen}
        onClose={() => setOpen(null)}
        returnFocus={opener}
      />
    </>
  );
}
