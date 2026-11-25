"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

import { originalFormat, pairOriginals } from "./file-originals";
import { filePreview, type FilePreview } from "./file-preview";
import { FilePreviewBody } from "./file-preview-body";
import {
  BADGE,
  CARD,
  DraftTile,
  type MediaAttachment,
  MediaTiles,
  MediaUrlContext,
  MediaViewer,
  type OpenHandler,
  TILE,
  TileFrame,
  useResolvedMedia,
  type ViewerFile,
  type ViewerItem,
} from "./media-attachments";
import type { AttachedFile } from "./pasted-text";
import { TextPreviewDialog } from "./pasted-text-card";

/** The file types that get their own icon; everything else is a plain file. */
export type FileIconKind = "pdf" | "sheet" | "doc" | "code" | "archive" | "file";

const ICON_KINDS: Record<Exclude<FileIconKind, "file">, readonly string[]> = {
  pdf: ["pdf"],
  sheet: ["csv", "tsv", "xls", "xlsx", "xlsm", "ods", "numbers"],
  doc: ["doc", "docx", "odt", "rtf", "txt", "md", "pages"],
  code: [
    "c",
    "cpp",
    "cs",
    "css",
    "go",
    "h",
    "html",
    "ipynb",
    "java",
    "js",
    "json",
    "jsx",
    "kt",
    "php",
    "py",
    "rb",
    "rs",
    "sh",
    "sql",
    "swift",
    "toml",
    "ts",
    "tsx",
    "xml",
    "yaml",
    "yml",
  ],
  archive: ["7z", "bz2", "gz", "rar", "tar", "tgz", "zip"],
};

/** A file name's extension in lower case, or "" when it has none. */
export function fileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 && dot < name.length - 1 ? name.slice(dot + 1).toLowerCase() : "";
}

/** The icon a file gets, picked from its extension. */
export function fileIconKind(name: string): FileIconKind {
  const ext = fileExtension(name);
  for (const [kind, exts] of Object.entries(ICON_KINDS))
    if (exts.includes(ext)) return kind as FileIconKind;
  return "file";
}

/** The last characters a shortened name keeps, so the extension stays in view. */
export const NAME_TAIL = 12;
/** The longest name a tile caption shows whole; about four lines at 12px in a 120px tile. */
export const NAME_MAX = 56;

/** A name cut to `max` characters with "…" in the middle; the last 12 characters stay. */
export function shortFileName(name: string, max = NAME_MAX): string {
  const chars = Array.from(name);
  if (chars.length <= max) return name;
  return `${chars.slice(0, max - NAME_TAIL - 1).join("")}…${chars.slice(-NAME_TAIL).join("")}`;
}

/** How an attached file's text is shown, or null when it has nothing to show. */
export function fileTilePreview(name: string, text: string): FilePreview | null {
  return filePreview(fileExtension(name), fileIconKind(name) === "code", text);
}

/**
 * An attached file, as Claude draws one: a 120px tile with the name as a
 * caption and the type as a badge. A click opens the file's preview, or calls
 * `onOpen` when a shared viewer shows it. A file with nothing to show is not a button.
 */
export function FileTile({
  name,
  text,
  preview: given,
  onRemove,
  onOpen,
}: {
  name: string;
  text: string;
  /** The preview a shared viewer already parsed, so the tile does not parse the text again. */
  preview?: FilePreview | null;
  onRemove?: () => void;
  onOpen?: OpenHandler;
}) {
  const [open, setOpen] = React.useState(false);
  const ext = fileExtension(name);
  const preview = React.useMemo(
    () => (given === undefined ? fileTilePreview(name, text) : given),
    [given, name, text],
  );
  const face = (
    <>
      <span
        dir="auto"
        className="line-clamp-4 break-words text-xs leading-tight text-foreground [overflow-wrap:anywhere]"
      >
        {shortFileName(name)}
      </span>
      {ext && <span className={cn(BADGE, "bg-muted uppercase")}>{ext}</span>}
    </>
  );
  const frame = cn(CARD, TILE, "flex flex-col bg-card p-2.5 text-start");
  return (
    <TileFrame name={name} onRemove={onRemove}>
      {/* A shared viewer may show a file with no text preview, such as its original. */}
      {preview || onOpen ? (
        <button
          type="button"
          onClick={onOpen ?? (() => setOpen(true))}
          aria-label={msg("shared.agent.media_open", { name })}
          title={name}
          className={cn(frame, "cursor-pointer")}
        >
          {face}
        </button>
      ) : (
        <div title={name} className={cn(frame, "hover:border-border")}>
          {face}
        </div>
      )}
      {preview && !onOpen && (
        <TextPreviewDialog open={open} onOpenChange={setOpen} title={name} text={text}>
          <FilePreviewBody preview={preview} />
        </TextPreviewDialog>
      )}
    </TileFrame>
  );
}

/**
 * The viewer slide of each attached file, in order, with its original; null
 * for a file with nothing to show. A file card's id links its original. A file
 * sent before SKY-782 has no id; it pairs by name with an original that has no
 * card. A sent message and the draft page the same way.
 */
export function fileSlides(
  files: readonly AttachedFile[],
  originals: readonly MediaAttachment[],
  previews: ReadonlyArray<FilePreview | null> = files.map((f) => fileTilePreview(f.name, f.text)),
): Array<ViewerFile | null> {
  const legacy = pairOriginals(
    files.filter((f) => !f.id),
    originals.filter((o) => !o.card),
  );
  let j = 0;
  return files.map((file, i) => {
    const preview = previews[i] ?? null;
    const original = file.id
      ? originalFormat(file.name)
        ? originals.find((o) => o.card === file.id)
        : undefined
      : (legacy[j++] ?? undefined);
    return preview || original
      ? { kind: "file", id: `file-${i}-${file.name}`, name: file.name, preview, original }
      : null;
  });
}

/**
 * A function from files to their previews, in order, that parses a file again
 * only when its name or text changes. It keeps only the last call's previews.
 */
export function previewCache(preview = fileTilePreview) {
  type Entry = { text: string; value: FilePreview | null };
  // A list per name: two files can share a name and differ in text.
  let kept = new Map<string, Entry[]>();
  return (files: readonly AttachedFile[]): Array<FilePreview | null> => {
    const next = new Map<string, Entry[]>();
    const out = files.map(({ name, text }) => {
      const same = (e: Entry) => e.text === text;
      const fresh = next.get(name) ?? [];
      const hit = fresh.find(same) ?? kept.get(name)?.find(same);
      const value = hit ? hit.value : preview(name, text);
      if (!fresh.some(same)) next.set(name, [...fresh, { text, value }]);
      return value;
    });
    kept = next;
    return out;
  };
}

// A draft original plays from its own object URL, as the draft's media do.
const draftUrl = (media: MediaAttachment) => Promise.resolve(media.url);

const NO_MEDIA: readonly MediaAttachment[] = [];

/**
 * The composer's attached media and files, media first. With `originals`, the
 * local copies of the draft's PDF, DOCX and XLSX originals that go out on Send,
 * every tile opens one shared viewer that pages through the media and the files,
 * as a sent message's tiles do, and a file there shows its original. Without it,
 * the media share their own viewer and each file opens its own text preview.
 */
export function DraftAttachments({
  media = NO_MEDIA,
  onRemoveMedia,
  files,
  originals,
  onRemove,
}: {
  media?: readonly MediaAttachment[];
  onRemoveMedia?: (id: string) => void;
  files: readonly AttachedFile[];
  originals?: readonly MediaAttachment[];
  onRemove: (index: number) => void;
}) {
  if (originals)
    return (
      <SharedDraftViewer
        media={media}
        onRemoveMedia={onRemoveMedia}
        files={files}
        originals={originals}
        onRemove={onRemove}
      />
    );
  return (
    <>
      {media.length > 0 && <MediaTiles media={media} onRemove={onRemoveMedia} bare />}
      {files.map((file, i) => (
        <FileTile
          key={`file-${i}`}
          name={file.name}
          text={file.text}
          onRemove={() => onRemove(i)}
        />
      ))}
    </>
  );
}

/** The draft's tiles with one viewer for its media and files; see `DraftAttachments`. */
function SharedDraftViewer({
  media: given,
  onRemoveMedia,
  files,
  originals,
  onRemove,
}: {
  media: readonly MediaAttachment[];
  onRemoveMedia?: (id: string) => void;
  files: readonly AttachedFile[];
  originals: readonly MediaAttachment[];
  onRemove: (index: number) => void;
}) {
  const media = useResolvedMedia(given);
  // The open slide's id: an index would move to another slide as tiles come and go.
  const [open, setOpen] = React.useState<string | null>(null);
  const opener = React.useRef<HTMLElement | null>(null);
  // The draft gives new file arrays on each key press; the cache keeps the parsed previews.
  const [previewsOf] = React.useState(previewCache);
  const { byFile, slides } = React.useMemo(() => {
    const byFile = fileSlides(files, originals, previewsOf(files));
    // As in MediaTiles, audio plays in its tile, and a failed upload still plays from its local URL.
    const visual: ViewerItem[] = media.filter((m) => m.kind !== "audio");
    return { byFile, slides: [...visual, ...byFile.filter((s): s is ViewerFile => s !== null)] };
  }, [media, files, originals, previewsOf]);
  const openAt = (id: string, e: React.MouseEvent<HTMLElement>) => {
    opener.current = e.currentTarget;
    setOpen(id);
  };
  const openIndex = open === null ? -1 : slides.findIndex((s) => s.id === open);
  // A slide that leaves gives up its id, so a later file at the same place does not open by itself.
  if (open !== null && openIndex < 0) setOpen(null);
  return (
    <>
      {media.map((m) => (
        <DraftTile
          key={m.id}
          media={m}
          onOpen={(e) => openAt(m.id, e)}
          onRemove={onRemoveMedia && (() => onRemoveMedia(m.id))}
        />
      ))}
      {files.map((file, i) => {
        const slide = byFile[i];
        return (
          <FileTile
            key={`file-${i}`}
            name={file.name}
            text={file.text}
            preview={slide?.preview ?? null}
            onRemove={() => onRemove(i)}
            onOpen={slide ? (e) => openAt(slide.id, e) : undefined}
          />
        );
      })}
      <MediaUrlContext.Provider value={draftUrl}>
        <MediaViewer
          media={slides}
          index={openIndex < 0 ? null : openIndex}
          onIndex={(i) => setOpen(slides[i]?.id ?? null)}
          onClose={() => setOpen(null)}
          returnFocus={opener}
        />
      </MediaUrlContext.Provider>
    </>
  );
}
