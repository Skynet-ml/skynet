"use client";

import * as React from "react";
import {
  ArrowClockwise,
  CaretLeft,
  CaretRight,
  CircleNotch,
  DownloadSimple,
  Headphones,
  MagnifyingGlassMinus,
  MagnifyingGlassPlus,
  Pause,
  Play,
  WarningCircle,
  X,
} from "@/shared/ui/icons";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { Dialog as DialogPrimitive } from "radix-ui";

import type { FilePreview } from "./file-preview";
import { TooltipButton } from "@/shared/ui/tooltip-button";

import { FilePage } from "./file-preview-body";

export type MediaKind = "image" | "video" | "audio";

/** An image, video or audio file that goes with a message and renders as itself, not as text. */
export interface MediaAttachment {
  id: string;
  name: string;
  kind: MediaKind;
  /** Where the file plays from: an object URL in the draft, a stored URL once sent. */
  url: string;
  /** The upload failed, or the stored file could not be fetched. */
  status?: "failed";
}

/** An attached file the viewer shows as a readable page: code, a table, Markdown or text. */
export interface ViewerFile {
  kind: "file";
  id: string;
  name: string;
  preview: FilePreview;
}

/** One slide of the viewer. */
export type ViewerItem = MediaAttachment | ViewerFile;

/**
 * Whether a file is a HEIC, HEIF or AVIF photo. The server refuses these, so the chat says so
 * before any upload. Some browsers give HEIC no type, so the name counts too.
 */
export function isUnsupportedImage(file: { type: string; name: string }): boolean {
  return /^image\/(heic|heif|avif)/i.test(file.type) || /\.(heic|heif|avif)$/i.test(file.name);
}

/** The media kind of a file, or null when the file is not image, video or audio. */
export function mediaKind(file: { type: string }): MediaKind | null {
  // An SVG is XML text that the model reads as text; the server does not take it as an image.
  if (file.type === "image/svg+xml") return null;
  const top = file.type.split("/")[0];
  return top === "image" || top === "video" || top === "audio" ? top : null;
}

type IconSlot =
  | "remove"
  | "play"
  | "pause"
  | "previous"
  | "next"
  | "download"
  | "close"
  | "failed"
  | "retry";
type SlotIcon = React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;

/** The icons the tiles and the viewer draw; a host can swap in its own set. */
export const MediaIconsContext = React.createContext<Record<IconSlot, SlotIcon>>({
  remove: X,
  play: (p) => <Play weight="fill" {...p} />,
  pause: (p) => <Pause weight="fill" {...p} />,
  previous: CaretLeft,
  next: CaretRight,
  download: DownloadSimple,
  close: X,
  failed: WarningCircle,
  retry: ArrowClockwise,
});

/**
 * Turns an attachment into a URL the page can play. A host sets it when a sent file's URL can
 * go stale (a reloaded chat keeps dead object URLs) or needs auth to fetch. Tiles wait for it.
 */
export const MediaUrlContext = React.createContext<
  ((media: MediaAttachment) => Promise<string>) | null
>(null);

/**
 * The media with URLs from the nearest MediaUrlContext. Files whose URL is not
 * ready yet drop out; files whose URL fails come back marked as failed.
 */
export function useResolvedMedia(media: readonly MediaAttachment[]): readonly MediaAttachment[] {
  const resolve = React.useContext(MediaUrlContext);
  // An empty string marks a file whose URL could not be resolved.
  const [urls, setUrls] = React.useState<Readonly<Record<string, string>>>({});
  React.useEffect(() => {
    if (!resolve) return;
    let live = true;
    for (const m of media) {
      resolve(m).then(
        (url) => live && setUrls((prev) => (prev[m.id] === url ? prev : { ...prev, [m.id]: url })),
        () => live && setUrls((prev) => (m.id in prev ? prev : { ...prev, [m.id]: "" })),
      );
    }
    return () => {
      live = false;
    };
  }, [resolve, media]);
  return React.useMemo(() => {
    if (!resolve) return media;
    return media.flatMap((m) => {
      const url = urls[m.id];
      if (url === undefined) return [];
      return [url ? { ...m, url } : { ...m, status: "failed" as const }];
    });
  }, [resolve, media, urls]);
}

/** Seconds as m:ss; 0:00 until the length is known. */
function clock(seconds: number): string {
  if (!Number.isFinite(seconds)) return "0:00";
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

/** Where along an element a pointer sits, from the reading start: 0..1, mirrored in RTL. */
function fractionAt(e: React.PointerEvent<Element>): number {
  const box = e.currentTarget.getBoundingClientRect();
  const f = (e.clientX - box.left) / box.width;
  return Math.min(
    1,
    Math.max(0, getComputedStyle(e.currentTarget).direction === "rtl" ? 1 - f : f),
  );
}

/**
 * The frame every attachment card shares. Skynet is flat, so a 1px border and
 * the surface tone separate the card, not a shadow; the border firms up on hover.
 */
export const CARD = cn(
  "relative shrink-0 overflow-hidden rounded-xl border border-border bg-muted",
  "transition-colors duration-150 hover:border-input",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
);

/** The 120px square of a composer tile, as Claude draws it. */
export const TILE = "size-[7.5rem]";

/** The small label in a tile's bottom start corner: a length or a file type. */
export const BADGE =
  "pointer-events-none absolute bottom-2 start-2 z-[1] max-w-[calc(100%-1rem)] truncate rounded-md bg-background/90 px-1.5 py-1 text-[11px] font-medium leading-none tabular-nums text-foreground";

/** The X in a tile's top end corner. It shows on hover or focus, and always on touch screens. */
function RemoveButton({ name, onRemove }: { name: string; onRemove: () => void }) {
  const Remove = React.useContext(MediaIconsContext).remove;
  return (
    <button
      type="button"
      onClick={onRemove}
      aria-label={msg("shared.agent.media_remove", { name })}
      title={msg("shared.agent.media_remove", { name })}
      className={cn(
        "absolute -end-2 -top-2 z-[2] flex size-5 cursor-pointer items-center justify-center rounded-full border border-border bg-background text-muted-foreground transition-[opacity,color] duration-150 hover:text-foreground",
        // A ring in the surface tone keeps a gap round the X, so it reads on any image.
        "ring-2 ring-background",
        // The hit area reaches a little past the 20px circle.
        "before:absolute before:-inset-1 before:content-['']",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
        "opacity-0 group-hover/tile:opacity-100 group-focus-within/tile:opacity-100 any-pointer-coarse:opacity-100 [@media(hover:none)]:opacity-100",
      )}
    >
      <Remove className="size-3" aria-hidden />
    </button>
  );
}

/**
 * One composer tile with its remove X. Delete or Backspace on the focused tile
 * removes it; the row then moves the focus to the next tile.
 */
export function TileFrame({
  name,
  onRemove,
  children,
}: {
  name: string;
  onRemove?: () => void;
  children: React.ReactNode;
}) {
  return (
    <div
      data-media-tile=""
      className="group/tile relative shrink-0"
      onKeyDown={(e) => {
        if (!onRemove || (e.key !== "Backspace" && e.key !== "Delete")) return;
        e.preventDefault();
        onRemove();
      }}
    >
      {children}
      {onRemove && <RemoveButton name={name} onRemove={onRemove} />}
    </div>
  );
}

const FADE_PX = 24;

/**
 * Claude's composer attachment row: one line that scrolls sideways and never
 * wraps. An edge fades only while more tiles sit past it.
 */
export function TileRow({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  const row = React.useRef<HTMLDivElement | null>(null);
  const refocus = React.useRef<number | null>(null);
  const [mask, setMask] = React.useState("");
  const measure = React.useCallback(() => {
    const el = row.current;
    if (!el) return;
    // RTL rows scroll to negative offsets, so the distance from the start is the absolute value.
    const from = Math.abs(el.scrollLeft);
    const start = from > 1;
    const end = from + el.clientWidth < el.scrollWidth - 1;
    const side = getComputedStyle(el).direction === "rtl" ? "left" : "right";
    setMask(
      start || end
        ? `linear-gradient(to ${side}, ${start ? "transparent" : "#000"}, #000 ${FADE_PX}px, #000 calc(100% - ${FADE_PX}px), ${end ? "transparent" : "#000"})`
        : "",
    );
  }, []);
  React.useEffect(() => {
    const el = row.current;
    if (!el) return;
    const frame = requestAnimationFrame(measure);
    // Tiles come and go without a resize, so the row measures again when its content changes.
    const observer = new MutationObserver(() => {
      measure();
      const at = refocus.current;
      refocus.current = null;
      if (at === null) return;
      const tiles = el.querySelectorAll<HTMLElement>("[data-media-tile]");
      const next = tiles[Math.min(at, tiles.length - 1)];
      next?.querySelector<HTMLElement>("button, a, [tabindex]")?.focus();
    });
    observer.observe(el, { childList: true, subtree: true });
    window.addEventListener("resize", measure);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [measure]);
  return (
    <div
      ref={row}
      onScroll={measure}
      onKeyDownCapture={(e) => {
        if (e.key !== "Backspace" && e.key !== "Delete") return;
        const tile = (e.target as Element).closest("[data-media-tile]");
        if (!tile) return;
        refocus.current = Array.from(e.currentTarget.querySelectorAll("[data-media-tile]")).indexOf(
          tile,
        );
      }}
      // The padding leaves room for each tile's X; the negative margin keeps the row in place.
      className={cn(
        "-mx-2 -mt-2.5 flex w-[calc(100%+1rem)] items-end gap-2 overflow-x-auto overscroll-x-contain px-2 pt-2.5 pb-0.5",
        "[scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
        className,
      )}
      style={mask ? { maskImage: mask, WebkitMaskImage: mask } : undefined}
    >
      {children}
    </div>
  );
}

/**
 * A video's first frame, with a play pill and the length on a bottom shade.
 * Inside a `group/video` control, hover pushes the frame in a little and
 * firms the pill, so the tile answers before the click.
 */
function VideoPoster({
  media,
  className,
  onError,
}: {
  media: MediaAttachment;
  className: string;
  onError?: () => void;
}) {
  const PlayIcon = React.useContext(MediaIconsContext).play;
  const [duration, setDuration] = React.useState(NaN);
  const time = Number.isFinite(duration) ? clock(duration) : "";
  return (
    <>
      {/* #t=0.1 asks the browser for the first frame as the poster. */}
      <video
        src={`${media.url}#t=0.1`}
        muted
        playsInline
        preload="metadata"
        tabIndex={-1}
        aria-hidden
        className={cn(
          className,
          "motion-safe:transition-transform motion-safe:duration-300 motion-safe:ease-[var(--ease-snappy)] motion-safe:group-hover/video:scale-[1.04]",
        )}
        // Metadata can load before React attaches onLoadedMetadata (after a server render).
        ref={(el) => {
          if (el && el.readyState >= 1) setDuration(el.duration);
        }}
        onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
        onError={onError}
      />
      <span className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-linear-to-t from-black/55 to-transparent" />
      <span className="pointer-events-none absolute bottom-2 start-2 z-[1] flex h-5 items-center gap-1 rounded-full bg-black/55 ps-1.5 pe-2 text-[11px] font-medium leading-none tabular-nums text-white transition-colors duration-150 group-hover/video:bg-black/75">
        <PlayIcon className="size-3" aria-hidden />
        {time}
      </span>
    </>
  );
}

/** An audio element with its play state, and its length read before the first play. */
function useAudio(media: MediaAttachment) {
  const ref = React.useRef<HTMLAudioElement | null>(null);
  const probing = React.useRef(false);
  const [playing, setPlaying] = React.useState(false);
  const [time, setTime] = React.useState(0);
  const [duration, setDuration] = React.useState(NaN);
  // Codex swaps play for a download link when the browser cannot play the file.
  const [broken, setBroken] = React.useState(false);
  React.useEffect(() => {
    const el = ref.current;
    // Metadata can load before React attaches onLoadedMetadata (after a server render).
    if (el && el.readyState >= 1 && Number.isFinite(el.duration)) setDuration(el.duration);
    return () => el?.pause();
  }, []);
  const toggle = () => {
    const el = ref.current;
    if (!el) return;
    if (el.paused) void el.play().catch(() => setBroken(true));
    else el.pause();
  };
  const seek = (fraction: number) => {
    const el = ref.current;
    if (el && Number.isFinite(el.duration)) el.currentTime = fraction * el.duration;
  };
  const element = (
    <audio
      ref={ref}
      src={media.url}
      preload="metadata"
      onPlay={() => setPlaying(true)}
      onPause={() => setPlaying(false)}
      onTimeUpdate={(e) => !probing.current && setTime(e.currentTarget.currentTime)}
      onLoadedMetadata={(e) => {
        const el = e.currentTarget;
        // A recorded WebM or Opus clip reports no length until the browser reads to
        // its end, so the card seeks far ahead once to learn it, then goes back.
        if (el.duration === Infinity) {
          probing.current = true;
          el.currentTime = Number.MAX_SAFE_INTEGER;
        } else setDuration(el.duration);
      }}
      onDurationChange={(e) => {
        const el = e.currentTarget;
        if (!Number.isFinite(el.duration)) return;
        setDuration(el.duration);
        if (!probing.current) return;
        probing.current = false;
        el.currentTime = 0;
      }}
      // At the end the clip goes back to the start, so the card shows the full length again.
      onEnded={(e) => {
        e.currentTarget.currentTime = 0;
        setTime(0);
      }}
      onError={() => setBroken(true)}
    />
  );
  return { element, playing, time, duration, broken, toggle, seek };
}

/** The play button of an audio tile or stage, or a download link when the browser cannot play the file. */
function AudioButton({
  media,
  audio,
  className,
  iconClassName,
}: {
  media: MediaAttachment;
  audio: ReturnType<typeof useAudio>;
  /** The button's size, shape and colors. */
  className: string;
  iconClassName: string;
}) {
  const icons = React.useContext(MediaIconsContext);
  const box = cn(
    "flex shrink-0 cursor-pointer items-center justify-center rounded-full transition-[transform,background-color] duration-75 active:scale-[0.97] motion-reduce:active:scale-100 focus-visible:outline-none focus-visible:ring-2",
    className,
  );
  if (audio.broken) {
    const label = msg("shared.agent.media_download_name", { name: media.name });
    return (
      <a href={media.url} download={media.name} aria-label={label} title={label} className={box}>
        <icons.download className={iconClassName} aria-hidden />
      </a>
    );
  }
  const Icon = audio.playing ? icons.pause : icons.play;
  const label = msg(audio.playing ? "shared.agent.media_pause" : "shared.agent.media_play", {
    name: media.name,
  });
  return (
    <button type="button" onClick={audio.toggle} aria-label={label} title={label} className={box}>
      {/* The new key replays the entrance, so play and pause swap with a short fade and grow. */}
      <Icon
        key={audio.playing ? "pause" : "play"}
        className={cn(iconClassName, "animate-in fade-in-0 duration-150 motion-safe:zoom-in-75")}
        aria-hidden
      />
    </button>
  );
}

/** What an audio tile shows as its time: the played time once it plays, else the length. */
function audioClock(audio: ReturnType<typeof useAudio>): string {
  if (audio.playing || audio.time) return clock(audio.time);
  return Number.isFinite(audio.duration) ? clock(audio.duration) : "";
}

/** A stable bar pattern for a clip's waveform, from its name: 0.25..1 per bar. */
function waveBars(name: string, count = 20): number[] {
  let seed = 0;
  for (const c of name) seed = (seed * 31 + c.charCodeAt(0)) >>> 0;
  return Array.from({ length: count }, (_, i) => {
    const x = Math.sin((seed % 1000) + i * 1.7) * 10000;
    return 0.25 + 0.75 * (x - Math.floor(x));
  });
}

/**
 * A clip's waveform that fills as it plays; a press seeks. The bar under the
 * playhead stands a little taller, so the eye finds the place. Hover tints the
 * bars up to the pointer, to show where a press will seek. It is a slider:
 * the arrow keys step 5 seconds, mirrored in RTL, and Home and End jump to the ends.
 */
function Waveform({
  name,
  audio,
  count,
  tones,
  className,
}: {
  name: string;
  audio: ReturnType<typeof useAudio>;
  count: number;
  /** The color classes of played bars, bars up to the pointer, and the rest. */
  tones: { played: string; aim: string; rest: string };
  className: string;
}) {
  const bars = React.useMemo(() => waveBars(name, count), [name, count]);
  const [aim, setAim] = React.useState<number | null>(null);
  const progress =
    Number.isFinite(audio.duration) && audio.duration > 0
      ? Math.min(1, audio.time / audio.duration)
      : 0;
  const head = audio.playing ? Math.min(bars.length - 1, Math.floor(progress * bars.length)) : -1;
  const known = Number.isFinite(audio.duration) && audio.duration > 0;
  return (
    <span
      role="slider"
      tabIndex={0}
      aria-label={msg("shared.agent.media_seek_audio")}
      aria-valuemin={0}
      aria-valuemax={known ? Math.round(audio.duration) : 0}
      aria-valuenow={Math.round(audio.time)}
      aria-valuetext={clock(audio.time)}
      className={cn(
        "flex cursor-pointer items-center rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
        className,
      )}
      onKeyDown={(e) => {
        if (!known) return;
        const rtl = getComputedStyle(e.currentTarget).direction === "rtl";
        let at: number;
        if (e.key === "Home") at = 0;
        else if (e.key === "End") at = audio.duration;
        else if (e.key === "ArrowLeft" || e.key === "ArrowRight")
          at = audio.time + ((e.key === "ArrowRight") !== rtl ? 5 : -5);
        else return;
        // The viewer pages on the same keys, so the slider keeps them.
        e.preventDefault();
        e.stopPropagation();
        audio.seek(Math.min(1, Math.max(0, at / audio.duration)));
      }}
      onPointerDown={(e) => audio.seek(fractionAt(e))}
      onPointerMove={(e) => setAim(fractionAt(e))}
      onPointerLeave={() => setAim(null)}
    >
      {bars.map((h, i) => {
        const at = (i + 0.5) / bars.length;
        return (
          <span
            key={i}
            className={cn(
              "h-[var(--bar)] min-w-0 flex-1 rounded-full transition-colors duration-150",
              "motion-safe:transition-[background-color,transform] motion-safe:ease-[var(--ease-snappy)]",
              at <= progress ? tones.played : aim !== null && at <= aim ? tones.aim : tones.rest,
              i === head && "motion-safe:scale-y-[1.25]",
            )}
            style={{ "--bar": `${Math.round(h * 100)}%` } as React.CSSProperties}
          />
        );
      })}
    </span>
  );
}

/** Audio as a 120px tile: the name, a waveform that fills as the clip plays, and play with the time. */
function AudioTile({ media }: { media: MediaAttachment }) {
  const audio = useAudio(media);
  return (
    <div className={cn(CARD, TILE, "flex flex-col gap-1.5 bg-card p-2.5 hover:border-border")}>
      {audio.element}
      <span
        dir="auto"
        title={media.name}
        className="truncate text-xs leading-tight text-foreground"
      >
        {media.name}
      </span>
      <Waveform
        name={media.name}
        audio={audio}
        count={20}
        tones={{ played: "bg-primary", aim: "bg-primary/35", rest: "bg-foreground/20" }}
        className="min-h-0 flex-1 gap-[2px]"
      />
      <span className="flex items-center justify-between">
        <AudioButton
          media={media}
          audio={audio}
          className="size-7 bg-primary-fill text-primary-foreground focus-visible:ring-primary/40"
          iconClassName="size-3.5"
        />
        <span
          data-audio-time=""
          className="text-[11px] leading-none tabular-nums text-muted-foreground"
        >
          {audioClock(audio)}
        </span>
      </span>
    </div>
  );
}

/** A media file that did not load: a tile in the danger tint, never a broken image icon. */
export function FailedTile({ media, className }: { media: MediaAttachment; className?: string }) {
  const Warning = React.useContext(MediaIconsContext).failed;
  return (
    <span
      role="img"
      aria-label={msg(
        media.kind === "video"
          ? "shared.agent.media_failed_video"
          : media.kind === "audio"
            ? "shared.agent.media_failed_audio"
            : "shared.agent.media_failed_image",
      )}
      title={media.name}
      className={cn(
        CARD,
        TILE,
        "flex flex-col items-center justify-center gap-1.5 border-destructive/30 bg-destructive/10 p-2 text-destructive hover:border-destructive/30",
        className,
      )}
    >
      <Warning className="size-5" aria-hidden />
      <span dir="auto" className="w-full truncate text-center text-xs">
        {media.name}
      </span>
    </span>
  );
}

/** A sent file whose URL is still on its way: a pulsing tile in the same place and size. */
export function PendingTile({ media }: { media: MediaAttachment }) {
  return (
    <span
      role="progressbar"
      aria-label={msg("shared.agent.media_loading_preview", { name: media.name })}
      title={media.name}
      className={cn(CARD, TILE, "block animate-pulse motion-reduce:animate-none")}
    />
  );
}

/**
 * A composer image: a pulsing skeleton until the preview decodes, then the
 * picture. An image that cannot decode shows the failed tile instead.
 */
function DraftImage({ media, onOpen }: { media: MediaAttachment; onOpen: OpenHandler }) {
  const [state, setState] = React.useState<"loading" | "loaded" | "error">("loading");
  if (state === "error") return <FailedTile media={media} />;
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={msg("shared.agent.media_open", { name: media.name })}
      title={media.name}
      className={cn(CARD, TILE, "flex cursor-zoom-in")}
    >
      {state === "loading" && (
        <span
          role="progressbar"
          aria-label={msg("shared.agent.media_loading_preview", { name: media.name })}
          className="absolute inset-0 animate-pulse bg-muted motion-reduce:animate-none"
        />
      )}
      {/* A plain img: object URLs and stored files skip the image optimizer. */}
      <img
        src={media.url}
        alt=""
        draggable={false}
        // A cached image can finish before React attaches onLoad (after a
        // server render), so a mounted image that is already done counts too.
        ref={(el) => {
          if (el?.complete) setState(el.naturalWidth ? "loaded" : "error");
        }}
        onLoad={() => setState("loaded")}
        onError={() => setState("error")}
        className={cn(
          "size-full object-cover motion-safe:transition-opacity motion-safe:duration-150",
          state === "loading" && "opacity-0",
        )}
      />
    </button>
  );
}

/** Opens the viewer; the control that fires it gets the focus back when the viewer closes. */
export type OpenHandler = (e: React.MouseEvent<HTMLElement>) => void;

/** One media file in the composer row: a 120px tile with the remove X. */
export function DraftTile({
  media,
  onOpen,
  onRemove,
}: {
  media: MediaAttachment;
  onOpen: OpenHandler;
  onRemove?: () => void;
}) {
  const failed = media.status === "failed";
  return (
    <TileFrame name={media.name} onRemove={onRemove}>
      {media.kind === "image" && <DraftImage media={media} onOpen={onOpen} />}
      {media.kind === "video" && (
        <button
          type="button"
          onClick={onOpen}
          aria-label={msg("shared.agent.media_play_video", { name: media.name })}
          title={media.name}
          className={cn(CARD, TILE, "group/video flex cursor-pointer")}
        >
          <VideoPoster media={media} className="size-full object-cover" />
        </button>
      )}
      {media.kind === "audio" && <AudioTile media={media} />}
      {/* No retry path exists yet, so a failed upload offers only the remove X. */}
      {failed && (
        <span className="pointer-events-none absolute inset-x-px bottom-px z-[1] rounded-b-[calc(var(--radius-xl)-1px)] border-t border-destructive/30 bg-background/95 px-2 py-1 text-center text-[11px] font-medium leading-tight text-destructive">
          {msg("shared.agent.media_upload_failed")}
        </span>
      )}
    </TileFrame>
  );
}

/** Images fill the viewer stage, so every item opens at the same size, as WhatsApp does. */
const VIEWER_IMAGE = "size-full select-none object-contain";
const MIN_ZOOM = 1;
const MAX_ZOOM = 8;

/** The zoom levels the viewer's zoom buttons step through. */
const ZOOM_STEP = 1.5;

/** Clamp a zoom level to the range the viewer allows. */
function clampZoom(zoom: number): number {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
}

/** Keep a pan inside the zoomed image, so an edge never comes past the stage's edge. */
function clampPan(pan: { x: number; y: number }, box: DOMRect, zoom: number) {
  const x = (box.width * (zoom - 1)) / 2;
  const y = (box.height * (zoom - 1)) / 2;
  return { x: Math.min(x, Math.max(-x, pan.x)), y: Math.min(y, Math.max(-y, pan.y)) };
}

/**
 * A zoomable, pannable image, as WhatsApp's viewer: a click zooms in at the
 * pointer and a second click zooms out, a drag pans, and Ctrl or Cmd with the
 * wheel or a pinch zooms. The viewer owns the zoom level, so its header
 * buttons and the image agree.
 */
function ZoomImage({
  src,
  alt,
  zoom,
  onZoom,
  onError,
}: {
  src: string;
  alt: string;
  zoom: number;
  onZoom: (zoom: number) => void;
  onError: () => void;
}) {
  const [pan, setPan] = React.useState({ x: 0, y: 0 });
  const [loaded, setLoaded] = React.useState(false);
  const drag = React.useRef<{ x: number; y: number; moved: boolean } | null>(null);
  const ref = React.useRef<HTMLDivElement | null>(null);
  const [panning, setPanning] = React.useState(false);
  if (zoom === 1 && (pan.x || pan.y)) setPan({ x: 0, y: 0 });
  // React's onWheel is passive, so preventDefault (no page zoom) needs a native listener.
  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      // Codex's ramp: each wheel unit scales by exp(-delta / 200).
      onZoom(clampZoom(zoom * Math.exp(-e.deltaY / 200)));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoom, onZoom]);
  return (
    <div
      ref={ref}
      className={cn(
        "flex size-full touch-none items-center justify-center overflow-hidden",
        zoom > 1 ? "cursor-grab active:cursor-grabbing" : "cursor-zoom-in",
      )}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        e.currentTarget.setPointerCapture(e.pointerId);
        // A zoom-out leaves the stored pan past the new edge; start from the clamped one.
        const p = clampPan(pan, e.currentTarget.getBoundingClientRect(), zoom);
        setPan(p);
        drag.current = { x: e.clientX - p.x, y: e.clientY - p.y, moved: false };
      }}
      onPointerMove={(e) => {
        const start = drag.current;
        if (!start || zoom === 1) return;
        const x = e.clientX - start.x;
        const y = e.clientY - start.y;
        // A few pixels of jitter is still a click, not a pan.
        if (!start.moved && Math.hypot(x - pan.x, y - pan.y) < 4) return;
        start.moved = true;
        setPanning(true);
        setPan(clampPan({ x, y }, e.currentTarget.getBoundingClientRect(), zoom));
      }}
      onPointerUp={(e) => {
        const start = drag.current;
        drag.current = null;
        setPanning(false);
        if (!start || start.moved) return;
        if (zoom > 1) return onZoom(1);
        // Zoom in on the clicked point: it stays under the pointer as the image grows.
        const box = e.currentTarget.getBoundingClientRect();
        const next = 2.5;
        setPan(
          clampPan(
            {
              x: (box.left + box.width / 2 - e.clientX) * (next - 1),
              y: (box.top + box.height / 2 - e.clientY) * (next - 1),
            },
            box,
            next,
          ),
        );
        onZoom(next);
      }}
      onPointerCancel={() => {
        drag.current = null;
        setPanning(false);
      }}
    >
      <img
        src={src}
        alt={alt}
        draggable={false}
        // The same cached-image case as the composer tile.
        ref={(el) => {
          if (el?.complete && el.naturalWidth) setLoaded(true);
        }}
        onLoad={() => setLoaded(true)}
        onError={onError}
        className={cn(
          VIEWER_IMAGE,
          "motion-safe:transition-[opacity,transform] motion-safe:duration-200 motion-safe:ease-[var(--ease-snappy)]",
          panning && "motion-safe:transition-none",
          !loaded && "opacity-0",
        )}
        // A zoom out from the header or the wheel can leave the stored pan too far; the
        // CSS clamp keeps the drawn image on the stage. Percents are of the image box.
        style={{
          transform: `translate(clamp(${-(zoom - 1) * 50}%, ${pan.x}px, ${(zoom - 1) * 50}%), clamp(${-(zoom - 1) * 50}%, ${pan.y}px, ${(zoom - 1) * 50}%)) scale(${zoom})`,
        }}
      />
    </div>
  );
}

/**
 * A thin media slider, as Codex draws its video timeline and volume: a 2px
 * track, a filled part and a round thumb. It mirrors in RTL. The host handles
 * the arrow keys, so the timeline can step 5 seconds.
 */
function Scrub({
  label,
  valueText,
  value,
  max,
  onChange,
  onKeyDown,
  onGrab,
  onRelease,
  className,
}: {
  label: string;
  valueText: string;
  value: number;
  max: number;
  onChange: (value: number) => void;
  onKeyDown?: (e: React.KeyboardEvent<HTMLDivElement>) => void;
  onGrab?: () => void;
  onRelease?: () => void;
  className?: string;
}) {
  const disabled = !(max > 0);
  const fill = disabled ? 0 : Math.min(1, value / max) * 100;
  const move = (e: React.PointerEvent<HTMLDivElement>) => onChange(fractionAt(e) * max);
  return (
    <div
      role="slider"
      tabIndex={disabled ? -1 : 0}
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max || 0}
      aria-valuenow={value}
      aria-valuetext={valueText}
      aria-disabled={disabled || undefined}
      className={cn(
        "group/scrub relative flex h-6 min-w-0 cursor-pointer touch-none items-center rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60",
        disabled && "cursor-default opacity-40",
        className,
      )}
      onPointerDown={(e) => {
        if (disabled || e.button !== 0) return;
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        e.currentTarget.focus({ preventScroll: true });
        onGrab?.();
        move(e);
      }}
      onPointerMove={(e) => e.currentTarget.hasPointerCapture(e.pointerId) && move(e)}
      onPointerUp={onRelease}
      onPointerCancel={onRelease}
      onKeyDown={onKeyDown}
    >
      <span className="relative h-0.5 w-full rounded-full bg-white/30">
        <span
          className="absolute inset-y-0 start-0 rounded-full bg-white"
          style={{ width: `${fill}%` }}
        />
      </span>
      <span
        aria-hidden
        className="absolute size-3 -translate-x-1/2 rounded-full bg-white shadow-md rtl:translate-x-1/2"
        style={{ insetInlineStart: `${fill}%` }}
      />
    </div>
  );
}

// WhatsApp's speed button cycles through these three rates, one step for each tap.
const SPEEDS = [1, 1.5, 2];
// Codex hides the overlay controls after 2.5 seconds without a pointer move, a press or a key.
const CONTROLS_IDLE_MS = 2500;
const SEEK_STEP_S = 5;
// A press on the video held this long plays it at 2× until the release, as in WhatsApp.
const HOLD_MS = 300;
const HOLD_RATE = 2;

/**
 * The viewer's video player, as WhatsApp's: a click or Space plays, a center
 * button plays while paused and replays at the end, a press held on the clip
 * plays it at 2×, and a spinner shows while the clip buffers. A floating glass
 * bar holds play, the elapsed time, the timeline, the time left and a speed
 * button that steps 1×, 1.5× and 2×. The time shows while the
 * pointer is on the bar. The bar hides while the clip plays and the pointer rests.
 */
function VideoPlayer({ src, name, onError }: { src: string; name: string; onError: () => void }) {
  const icons = React.useContext(MediaIconsContext);
  const box = React.useRef<HTMLDivElement | null>(null);
  const ref = React.useRef<HTMLVideoElement | null>(null);
  const grab = React.useRef<{ wasPlaying: boolean } | null>(null);
  const hold = React.useRef<{ timer: number; on: boolean } | null>(null);
  const skipClick = React.useRef(false);
  const [playing, setPlaying] = React.useState(false);
  const [time, setTime] = React.useState(0);
  const [duration, setDuration] = React.useState(0);
  const [rate, setRate] = React.useState(1);
  const [idle, setIdle] = React.useState(false);
  const [holding, setHolding] = React.useState(false);
  const [ended, setEnded] = React.useState(false);
  const [waiting, setWaiting] = React.useState(false);

  React.useEffect(() => {
    const el = box.current;
    if (!el || !playing) return;
    let timer = window.setTimeout(() => setIdle(true), CONTROLS_IDLE_MS);
    const wake = () => {
      setIdle(false);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => setIdle(true), CONTROLS_IDLE_MS);
    };
    for (const type of ["pointermove", "pointerdown", "keydown"]) el.addEventListener(type, wake);
    return () => {
      window.clearTimeout(timer);
      for (const type of ["pointermove", "pointerdown", "keydown"])
        el.removeEventListener(type, wake);
    };
  }, [playing]);

  React.useEffect(() => () => window.clearTimeout(hold.current?.timer), []);

  const toggle = () => {
    const el = ref.current;
    if (!el) return;
    if (el.paused) void el.play().catch(() => undefined);
    else el.pause();
  };
  const seekTo = (t: number) => {
    const el = ref.current;
    if (!el || !(duration > 0)) return;
    el.currentTime = Math.max(0, Math.min(duration, t));
    setTime(el.currentTime);
  };
  const nextSpeed = () => {
    const next = SPEEDS[(SPEEDS.indexOf(rate) + 1) % SPEEDS.length] ?? 1;
    setRate(next);
    if (ref.current && !hold.current?.on) ref.current.playbackRate = next;
  };
  const startHold = (e: React.PointerEvent<HTMLVideoElement>) => {
    const el = e.currentTarget;
    if (e.button !== 0 || el.paused) return;
    window.clearTimeout(hold.current?.timer);
    hold.current = {
      on: false,
      timer: window.setTimeout(() => {
        if (!hold.current) return;
        hold.current.on = true;
        el.playbackRate = HOLD_RATE;
        setHolding(true);
      }, HOLD_MS),
    };
  };
  const endHold = () => {
    const held = hold.current;
    if (!held) return;
    hold.current = null;
    window.clearTimeout(held.timer);
    if (!held.on) return;
    // The release of a hold is not a click: the clip keeps playing at its own speed.
    skipClick.current = true;
    if (ref.current) ref.current.playbackRate = rate;
    setHolding(false);
  };
  const progress = `${clock(time)} / ${clock(duration)}`;
  const Icon = playing ? icons.pause : icons.play;
  const playLabel = msg(playing ? "shared.agent.media_pause" : "shared.agent.media_play", { name });
  const Center = ended ? ArrowClockwise : icons.play;
  const centerLabel = ended ? msg("shared.agent.media_replay", { name }) : playLabel;
  const tool =
    "flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-full text-white/85 transition-colors hover:bg-white/15 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60";
  const label =
    "w-10 shrink-0 text-center text-xs tabular-nums text-white/80 opacity-0 transition-opacity duration-150 group-hover/bar:opacity-100 group-focus-within/bar:opacity-100";
  const hidden = playing && (idle || holding);
  return (
    <div
      ref={box}
      className={cn(
        "relative flex h-full max-w-full overflow-hidden bg-black",
        hidden && !holding && "cursor-none",
      )}
    >
      <video
        ref={ref}
        src={src}
        autoPlay
        playsInline
        tabIndex={0}
        aria-label={name}
        onError={onError}
        className="block h-full w-auto max-w-full cursor-pointer object-contain focus-visible:outline-none"
        onClick={() => {
          if (skipClick.current) skipClick.current = false;
          else toggle();
        }}
        onPointerDown={startHold}
        onPointerUp={endHold}
        onPointerCancel={endHold}
        onPointerLeave={endHold}
        onContextMenu={(e) => hold.current?.on && e.preventDefault()}
        onKeyDown={(e) => {
          if (e.key !== " " && e.key !== "Enter") return;
          e.preventDefault();
          if (!e.repeat) toggle();
        }}
        onDurationChange={(e) =>
          setDuration(Number.isFinite(e.currentTarget.duration) ? e.currentTarget.duration : 0)
        }
        onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
        onPlay={() => {
          setIdle(false);
          setEnded(false);
          setPlaying(true);
        }}
        onPause={() => {
          endHold();
          setPlaying(false);
        }}
        onEnded={() => {
          setPlaying(false);
          setEnded(true);
        }}
        onWaiting={() => setWaiting(true)}
        onPlaying={() => setWaiting(false)}
        onCanPlay={() => setWaiting(false)}
      />
      {playing && waiting && (
        <span
          role="status"
          aria-label={msg("shared.agent.media_buffering", { name })}
          className="pointer-events-none absolute left-1/2 top-1/2 flex size-16 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full bg-black/55 text-white motion-safe:animate-in motion-safe:fade-in-0 motion-safe:delay-300 motion-safe:fill-mode-backwards"
        >
          <CircleNotch className="size-7 motion-safe:animate-spin" aria-hidden />
        </span>
      )}
      {!playing && (
        <button
          type="button"
          onClick={toggle}
          aria-label={centerLabel}
          className="absolute left-1/2 top-1/2 flex size-16 -translate-x-1/2 -translate-y-1/2 cursor-pointer items-center justify-center rounded-full bg-black/55 text-white transition-[transform,background-color] duration-150 hover:bg-black/70 active:scale-[0.97] motion-safe:animate-in motion-safe:fade-in-0 motion-safe:zoom-in-90 motion-reduce:active:scale-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
        >
          <Center className={cn("size-7", !ended && "translate-x-px")} aria-hidden />
        </button>
      )}
      <div
        className={cn(
          "group/bar absolute inset-x-3 bottom-3 mx-auto flex max-w-xl items-center gap-0.5 rounded-full bg-neutral-800/70 p-1 text-white shadow-[var(--shadow-float)] ring-1 ring-white/10 backdrop-blur-md transition-opacity duration-200",
          hidden && "pointer-events-none opacity-0",
        )}
      >
        <TooltipButton tooltip={playLabel} side="top">
          <button type="button" onClick={toggle} aria-label={playLabel} className={tool}>
            <Icon className="size-4" aria-hidden />
          </button>
        </TooltipButton>
        <span dir="ltr" className={label}>
          {clock(time)}
        </span>
        <Scrub
          className="flex-1"
          label={msg("shared.agent.media_seek")}
          valueText={progress}
          value={Math.min(time, duration)}
          max={duration}
          onChange={seekTo}
          onKeyDown={(e) => {
            if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
            // The viewer's arrows switch items; on the timeline they seek instead.
            e.preventDefault();
            e.stopPropagation();
            // The timeline mirrors in RTL, so the arrow keys follow the side they point to.
            const forward =
              (e.key === "ArrowRight") !== (getComputedStyle(e.currentTarget).direction === "rtl");
            seekTo(time + (forward ? SEEK_STEP_S : -SEEK_STEP_S));
          }}
          onGrab={() => {
            const el = ref.current;
            if (!el || grab.current) return;
            grab.current = { wasPlaying: !el.paused };
            el.pause();
          }}
          onRelease={() => {
            const held = grab.current;
            grab.current = null;
            if (held?.wasPlaying) void ref.current?.play().catch(() => undefined);
          }}
        />
        <span dir="ltr" className={label}>
          {`-${clock(Math.max(0, duration - time))}`}
        </span>
        <TooltipButton tooltip={msg("shared.agent.media_speed")} side="top">
          <button
            type="button"
            onClick={nextSpeed}
            aria-label={`${msg("shared.agent.media_speed")}: ${rate}×`}
            className={cn(tool, "w-10 text-xs font-medium tabular-nums")}
          >
            {`${rate}×`}
          </button>
        </TooltipButton>
      </div>
    </div>
  );
}

/** A file in the viewer, drawn the way its own app shows it. */
function FileSlide({ file }: { file: ViewerFile }) {
  return (
    <div data-file-page="" className="flex h-full w-full items-center justify-center">
      <FilePage name={file.name} preview={file.preview} />
    </div>
  );
}

/**
 * Audio in the viewer, as WhatsApp shows an audio file: a round audio mark on
 * the dark stage, a large waveform that seeks, the played time and the length
 * at its two ends, and one round play button.
 */
function AudioSlide({ media }: { media: MediaAttachment }) {
  const audio = useAudio(media);
  return (
    <div className="flex h-full w-full max-w-xl flex-col items-center justify-center gap-8 text-white">
      {audio.element}
      <span className="flex size-28 items-center justify-center rounded-full bg-white/10 text-white/80">
        <Headphones className="size-12" aria-hidden />
      </span>
      <div className="flex w-full flex-col gap-2">
        <Waveform
          name={media.name}
          audio={audio}
          count={48}
          tones={{ played: "bg-white", aim: "bg-white/50", rest: "bg-white/25" }}
          className="h-16 w-full gap-1"
        />
        <span
          data-audio-time=""
          dir="ltr"
          className="flex justify-between text-xs tabular-nums text-white/70"
        >
          <span>{clock(audio.time)}</span>
          <span>{Number.isFinite(audio.duration) ? clock(audio.duration) : ""}</span>
        </span>
      </div>
      <AudioButton
        media={media}
        audio={audio}
        className="size-14 bg-white text-black hover:bg-white/90 focus-visible:ring-white/60"
        iconClassName="size-6"
      />
    </div>
  );
}

/** A slide next to the open one: a still preview that loads nothing heavy. */
function NeighbourSlide({ item }: { item: ViewerItem }) {
  // A file page can be a large table, so a neighbour shows only the file's type.
  if (item.kind === "file") return <Thumb item={item} className="size-24" />;
  if (item.kind === "audio") return <AudioSlide media={item} />;
  if (item.kind === "image")
    return <img src={item.url} alt="" draggable={false} className={VIEWER_IMAGE} />;
  return (
    <video
      src={`${item.url}#t=0.1`}
      muted
      preload="metadata"
      className="h-full w-auto max-w-full object-contain"
    />
  );
}

/** A file's extension in capitals, as a label: "PY", "MP4". */
function extLabel(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toUpperCase() : "";
}

/** A small square preview of a viewer item, for the header and the strip. */
function Thumb({ item, className }: { item: ViewerItem; className: string }) {
  const box = cn(
    "relative flex shrink-0 items-center justify-center overflow-hidden rounded-md bg-white/10 text-white/80",
    className,
  );
  if (item.kind === "image")
    return (
      <span className={box}>
        <img
          src={item.url}
          alt=""
          loading="lazy"
          draggable={false}
          className="size-full object-cover"
        />
      </span>
    );
  if (item.kind === "video")
    return (
      <span className={box}>
        <video
          src={`${item.url}#t=0.1`}
          muted
          playsInline
          preload="metadata"
          tabIndex={-1}
          aria-hidden
          className="size-full object-cover"
        />
        <Play
          weight="fill"
          className="absolute start-1 bottom-1 size-2.5 drop-shadow"
          aria-hidden
        />
      </span>
    );
  if (item.kind === "audio")
    return (
      <span className={cn(box, "gap-px px-2")}>
        {waveBars(item.name, 7).map((h, i) => (
          <span
            key={i}
            className="w-0.5 rounded-full bg-current"
            style={{ height: `${h * 50}%` }}
          />
        ))}
      </span>
    );
  return <span className={cn(box, "text-[9px] font-semibold")}>{extLabel(item.name)}</span>;
}

/** The open item in the viewer. Media that fails to load says so and offers Try again. */
function CurrentSlide({ item, zoom }: { item: ViewerItem; zoom: ViewerZoom }) {
  if (item.kind === "file") return <FileSlide file={item} />;
  if (item.kind === "audio") return <AudioSlide media={item} />;
  return <MediaSlide media={item} zoom={zoom} />;
}

/** The open image's zoom level, which the viewer header also sets. */
interface ViewerZoom {
  value: number;
  set: (zoom: number) => void;
}

/** The open image or video. A file that fails to load says so and offers Try again. */
function MediaSlide({ media, zoom }: { media: MediaAttachment; zoom: ViewerZoom }) {
  const Retry = React.useContext(MediaIconsContext).retry;
  const [attempt, setAttempt] = React.useState(0);
  const [failed, setFailed] = React.useState(false);
  if (failed)
    return (
      <div
        role="alert"
        className="flex flex-col items-center gap-3 rounded-xl bg-white/5 px-6 py-5 text-sm text-white/80"
      >
        <span>{msg("shared.agent.media_load_error")}</span>
        <button
          type="button"
          onClick={() => {
            setFailed(false);
            setAttempt((n) => n + 1);
          }}
          className="flex cursor-pointer items-center gap-1.5 rounded-lg bg-white/10 px-3 py-1.5 font-medium text-white transition-[transform,background-color] duration-75 hover:bg-white/15 active:scale-[0.97] motion-reduce:active:scale-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
        >
          <Retry className="size-4" aria-hidden />
          {msg("shared.agent.media_try_again")}
        </button>
      </div>
    );
  // A new key on Try again mounts a fresh element, so the browser fetches the file again.
  return media.kind === "image" ? (
    <ZoomImage
      key={`${media.id}-${attempt}`}
      src={media.url}
      alt={media.name}
      zoom={zoom.value}
      onZoom={zoom.set}
      onError={() => setFailed(true)}
    />
  ) : (
    <VideoPlayer
      key={`${media.id}-${attempt}`}
      src={media.url}
      name={media.name}
      onError={() => setFailed(true)}
    />
  );
}

const VIEWER_FADE =
  "data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 duration-150 motion-reduce:animate-none";

/**
 * WhatsApp's media viewer: a dark backdrop with one stage, the same size for
 * every item; an image, a video, an audio panel or a file page fills it. The
 * header shows the item, its type and "n of total", with download and close
 * at the end. Previous and next sit at the sides, and a strip of thumbnails
 * at the bottom jumps to any item. The slides slide into place. The arrow
 * keys, Home and End move between items, mirrored in RTL, and stop at the ends.
 */
export function MediaViewer({
  media,
  index: openIndex,
  onIndex,
  onClose,
  returnFocus,
}: {
  media: readonly ViewerItem[];
  index: number | null;
  onIndex: (i: number) => void;
  onClose: () => void;
  /** The control that opened the viewer; it gets the focus back on close. */
  returnFocus?: React.RefObject<HTMLElement | null>;
}) {
  const icons = React.useContext(MediaIconsContext);
  const strip = React.useRef<HTMLDivElement | null>(null);
  // The last open item stays on the stage while the viewer fades out.
  const [kept, setKept] = React.useState(openIndex);
  if (openIndex !== null && openIndex !== kept) setKept(openIndex);
  const index = openIndex ?? (kept !== null && kept < media.length ? kept : null);
  const [zoom, setZoom] = React.useState({ index, value: 1 });
  const item = index === null ? null : media[index];
  // Each item opens at its fitted size.
  const zoomValue = zoom.index === index ? zoom.value : 1;
  const setZoomValue = React.useCallback((value: number) => setZoom({ index, value }), [index]);
  React.useEffect(() => {
    // The strip keeps the open item's thumbnail centred as the viewer pages. It scrolls
    // only itself: scrollIntoView would also scroll the overflow-hidden dialog sideways.
    const row = strip.current;
    const thumb = row?.querySelector<HTMLElement>("[aria-current]");
    if (!row || !thumb) return;
    const delta =
      thumb.getBoundingClientRect().left +
      thumb.offsetWidth / 2 -
      (row.getBoundingClientRect().left + row.clientWidth / 2);
    row.scrollBy?.({ left: delta, behavior: "smooth" });
  }, [index]);
  const last = media.length - 1;
  const go = (i: number) => {
    if (index !== null && i >= 0 && i <= last && i !== index) onIndex(i);
  };
  const Previous = icons.previous;
  const Next = icons.next;
  const Download = icons.download;
  const Close = icons.close;
  const tool =
    "pointer-events-auto flex size-8 cursor-pointer items-center justify-center rounded-full text-white/70 transition-colors duration-150 hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60";
  const arrow = (enabled: boolean) =>
    cn(
      "absolute top-1/2 z-20 flex size-10 -translate-y-1/2 items-center justify-center rounded-full bg-white/10 transition-[opacity,background-color] duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60",
      enabled
        ? "cursor-pointer text-white hover:bg-white/20"
        : "cursor-default text-white/30 opacity-0 focus-visible:opacity-100",
    );
  const many = media.length > 1;
  return (
    <DialogPrimitive.Root
      open={openIndex !== null && item !== null}
      onOpenChange={(open) => !open && onClose()}
    >
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className={cn("fixed inset-0 z-50 bg-black/95", VIEWER_FADE)} />
        <DialogPrimitive.Content
          className={cn(
            "fixed inset-0 z-50 flex items-center overflow-hidden outline-none",
            VIEWER_FADE,
            "data-[state=open]:zoom-in-95 data-[state=closed]:zoom-out-95",
          )}
          onOpenAutoFocus={(e) => {
            // Focus on the first tool would open its tooltip, and Escape would close only that.
            e.preventDefault();
            (e.currentTarget as HTMLElement).focus();
          }}
          onCloseAutoFocus={(e) => {
            // Safari does not focus a clicked button, so the viewer names its opener.
            const el = returnFocus?.current;
            if (!el?.isConnected) return;
            e.preventDefault();
            el.focus();
          }}
          onClick={(e) => {
            // The media floats with no card, so a click on the dark space around it closes.
            const target = e.target as HTMLElement;
            if (target === e.currentTarget || target.dataset.slide !== undefined) onClose();
          }}
          onKeyDown={(e) => {
            // A slider took the key, or the focus is in a file page that scrolls on these keys.
            if (
              index === null ||
              e.defaultPrevented ||
              (e.target as HTMLElement).closest("[data-file-page]")
            )
              return;
            const rtl = getComputedStyle(e.currentTarget).direction === "rtl";
            if (e.key === "Home") go(0);
            else if (e.key === "End") go(last);
            else if (e.key === "ArrowLeft" || e.key === "ArrowRight")
              go(index + ((e.key === "ArrowRight") !== rtl ? 1 : -1));
            else return;
            e.preventDefault();
          }}
        >
          {item && index !== null && (
            <>
              <DialogPrimitive.Title className="sr-only">
                {msg(
                  item.kind === "file"
                    ? "shared.agent.media_preview_file"
                    : item.kind === "audio"
                      ? "shared.agent.media_preview_audio"
                      : item.kind === "video"
                        ? "shared.agent.media_preview_video"
                        : "shared.agent.media_preview_image",
                )}
              </DialogPrimitive.Title>
              <DialogPrimitive.Description className="sr-only">
                {item.name}
              </DialogPrimitive.Description>
              <div
                data-slide=""
                className="flex h-full shrink-0 [transform:translateX(var(--slide))] motion-safe:transition-transform motion-safe:duration-300 motion-safe:ease-[var(--ease-snappy)] rtl:[transform:translateX(calc(-1*var(--slide)))]"
                style={{ "--slide": `${-index * 100}vw` } as React.CSSProperties}
              >
                {media.map((m, i) => (
                  <div
                    key={m.id}
                    data-slide=""
                    inert={i !== index}
                    className={cn(
                      "flex h-full w-screen shrink-0 items-center justify-center px-14 pt-16 sm:px-20",
                      many ? "pb-28" : "pb-12",
                    )}
                  >
                    {/* Only the open item and its neighbours load. */}
                    {Math.abs(i - index) <= 1 &&
                      (i === index ? (
                        <CurrentSlide
                          key={m.id}
                          item={m}
                          zoom={{ value: zoomValue, set: setZoomValue }}
                        />
                      ) : (
                        <NeighbourSlide item={m} />
                      ))}
                  </div>
                ))}
              </div>
              <div className="pointer-events-none absolute inset-x-0 top-0 z-20 flex h-16 items-center gap-3 bg-linear-to-b from-black/80 to-transparent px-4">
                <Thumb item={item} className="size-9" />
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span dir="auto" className="truncate text-sm font-medium text-white">
                    {item.name}
                  </span>
                  <span className="flex items-center gap-1.5 text-xs tabular-nums text-white/60">
                    {extLabel(item.name)}
                    {many && (
                      <>
                        <span aria-hidden>·</span>
                        <span role="status">
                          {msg("shared.agent.media_position", {
                            index: index + 1,
                            count: media.length,
                          })}
                        </span>
                      </>
                    )}
                  </span>
                </div>
                {item.kind === "image" && (
                  <>
                    <TooltipButton tooltip={msg("shared.agent.media_zoom_out")} side="bottom">
                      <button
                        type="button"
                        onClick={() => setZoomValue(clampZoom(zoomValue / ZOOM_STEP))}
                        disabled={zoomValue <= MIN_ZOOM}
                        aria-label={msg("shared.agent.media_zoom_out")}
                        className={cn(
                          tool,
                          "disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent",
                        )}
                      >
                        <MagnifyingGlassMinus className="size-4" aria-hidden />
                      </button>
                    </TooltipButton>
                    <TooltipButton tooltip={msg("shared.agent.media_zoom_in")} side="bottom">
                      <button
                        type="button"
                        onClick={() => setZoomValue(clampZoom(zoomValue * ZOOM_STEP))}
                        disabled={zoomValue >= MAX_ZOOM}
                        aria-label={msg("shared.agent.media_zoom_in")}
                        className={cn(
                          tool,
                          "disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent",
                        )}
                      >
                        <MagnifyingGlassPlus className="size-4" aria-hidden />
                      </button>
                    </TooltipButton>
                  </>
                )}
                {item.kind !== "file" && (
                  <TooltipButton tooltip={msg("shared.agent.media_download")} side="bottom">
                    <a
                      href={item.url}
                      download={item.name}
                      aria-label={msg("shared.agent.media_download_name", { name: item.name })}
                      className={tool}
                    >
                      <Download className="size-4" aria-hidden />
                    </a>
                  </TooltipButton>
                )}
                <TooltipButton tooltip={msg("shared.dialog.close")} side="bottom">
                  <DialogPrimitive.Close aria-label={msg("shared.dialog.close")} className={tool}>
                    <Close className="size-4" aria-hidden />
                  </DialogPrimitive.Close>
                </TooltipButton>
              </div>
              {many && (
                <>
                  <TooltipButton
                    tooltip={msg("shared.agent.media_previous")}
                    side="top"
                    contentClassName={cn(index === 0 && "hidden")}
                  >
                    <button
                      type="button"
                      onClick={() => go(index - 1)}
                      aria-disabled={index === 0 || undefined}
                      aria-label={msg("shared.agent.media_previous")}
                      className={cn(arrow(index > 0), "start-2 sm:start-5")}
                    >
                      <Previous className="size-5 rtl:-scale-x-100" aria-hidden />
                    </button>
                  </TooltipButton>
                  <TooltipButton
                    tooltip={msg("shared.agent.media_next")}
                    side="top"
                    contentClassName={cn(index === last && "hidden")}
                  >
                    <button
                      type="button"
                      onClick={() => go(index + 1)}
                      aria-disabled={index === last || undefined}
                      aria-label={msg("shared.agent.media_next")}
                      className={cn(arrow(index < last), "end-2 sm:end-5")}
                    >
                      <Next className="size-5 rtl:-scale-x-100" aria-hidden />
                    </button>
                  </TooltipButton>
                  <div className="pointer-events-none absolute inset-x-0 bottom-0 z-20 flex justify-center bg-linear-to-t from-black/80 to-transparent pt-6 pb-4">
                    <div
                      ref={strip}
                      className="pointer-events-auto flex max-w-full gap-2 overflow-x-auto px-4 py-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
                    >
                      {media.map((m, i) => (
                        <TooltipButton key={m.id} tooltip={m.name} side="top">
                          <button
                            type="button"
                            onClick={() => go(i)}
                            aria-label={m.name}
                            aria-current={i === index || undefined}
                            className={cn(
                              "shrink-0 cursor-pointer rounded-md transition-[opacity,box-shadow] duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60",
                              i === index ? "ring-2 ring-white" : "opacity-50 hover:opacity-80",
                            )}
                          >
                            <Thumb item={m} className="size-12" />
                          </button>
                        </TooltipButton>
                      ))}
                    </div>
                  </div>
                </>
              )}
            </>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

/**
 * The composer's media: one scrolling row of 120px tiles. Images and videos
 * open one shared viewer; audio plays in place. Renders nothing when the list
 * is empty.
 */
export function MediaTiles({
  media: given,
  onRemove,
  bare = false,
  className,
}: {
  media: readonly MediaAttachment[];
  onRemove?: (id: string) => void;
  /** The tiles go into a row the caller owns, next to its own tiles. */
  bare?: boolean;
  className?: string;
}) {
  const [open, setOpen] = React.useState<number | null>(null);
  const media = useResolvedMedia(given);
  const opener = React.useRef<HTMLElement | null>(null);
  // A draft file that failed to upload still plays from its local URL, so it stays in the viewer.
  const visual = React.useMemo(() => media.filter((m) => m.kind !== "audio"), [media]);
  if (!media.length) return null;
  const tiles = media.map((m) => (
    <DraftTile
      key={m.id}
      media={m}
      onOpen={(e) => {
        opener.current = e.currentTarget;
        setOpen(visual.indexOf(m));
      }}
      onRemove={onRemove && (() => onRemove(m.id))}
    />
  ));
  return (
    <>
      {bare ? tiles : <TileRow className={className}>{tiles}</TileRow>}
      <MediaViewer
        media={visual}
        index={open === null || !visual.length ? null : Math.min(open, visual.length - 1)}
        onIndex={setOpen}
        onClose={() => setOpen(null)}
        returnFocus={opener}
      />
    </>
  );
}
