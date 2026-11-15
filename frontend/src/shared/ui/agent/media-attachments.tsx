"use client";

import * as React from "react";
import {
  ArrowClockwise,
  ArrowsIn,
  ArrowsOut,
  CaretLeft,
  CaretRight,
  Check,
  DotsThree,
  DownloadSimple,
  Pause,
  Play,
  SpeakerHigh,
  SpeakerSlash,
  WarningCircle,
  X,
} from "@/shared/ui/icons";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { Dialog as DialogPrimitive } from "radix-ui";

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
function useResolvedMedia(media: readonly MediaAttachment[]): readonly MediaAttachment[] {
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

/** The round play mark in the middle of a video poster. */
const PLAY_MARK =
  "flex size-9 items-center justify-center rounded-full bg-background/90 text-foreground";

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
    const frame = requestAnimationFrame(measure);
    window.addEventListener("resize", measure);
    const at = refocus.current;
    refocus.current = null;
    if (at !== null) {
      const tiles = row.current?.querySelectorAll<HTMLElement>("[data-media-tile]");
      const next = tiles?.[Math.min(at, tiles.length - 1)];
      next?.querySelector<HTMLElement>("button, a, [tabindex]")?.focus();
    }
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("resize", measure);
    };
  }, [children, measure]);
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

/** A video's first frame with a play mark in the middle and the length in the corner. */
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
        className={className}
        onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
        onError={onError}
      />
      <span className="pointer-events-none absolute inset-0 flex items-center justify-center">
        <span className={PLAY_MARK}>
          <PlayIcon className="size-4" aria-hidden />
        </span>
      </span>
      {Number.isFinite(duration) && <span className={BADGE}>{clock(duration)}</span>}
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

/** The square play button of an audio card, or a download link when the browser cannot play the file. */
function AudioButton({
  media,
  audio,
  tone = "fill",
}: {
  media: MediaAttachment;
  audio: ReturnType<typeof useAudio>;
  /** "chip" draws the file chip's tinted icon box; "fill" the solid tile button. */
  tone?: "fill" | "chip";
}) {
  const icons = React.useContext(MediaIconsContext);
  const box = cn(
    "flex size-9 shrink-0 cursor-pointer items-center justify-center rounded-lg transition-[transform,background-color] duration-75 active:scale-[0.97] motion-reduce:active:scale-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
    tone === "chip"
      ? "rounded-md bg-foreground/5 text-foreground hover:bg-foreground/10"
      : "bg-primary-fill text-primary-foreground",
  );
  if (audio.broken) {
    const label = msg("shared.agent.media_download_name", { name: media.name });
    return (
      <a href={media.url} download={media.name} aria-label={label} title={label} className={box}>
        <icons.download className="size-4" aria-hidden />
      </a>
    );
  }
  const Icon = audio.playing ? icons.pause : icons.play;
  const label = msg(audio.playing ? "shared.agent.media_pause" : "shared.agent.media_play", {
    name: media.name,
  });
  return (
    <button type="button" onClick={audio.toggle} aria-label={label} title={label} className={box}>
      <Icon className="size-4" aria-hidden />
    </button>
  );
}

/** What an audio card shows as its time: the played time once it plays, else the length. */
function audioClock(audio: ReturnType<typeof useAudio>): string {
  if (audio.playing || audio.time) return clock(audio.time);
  return Number.isFinite(audio.duration) ? clock(audio.duration) : "";
}

/** Audio in the composer: a 120px tile with the name, play in the middle and the length. */
function AudioTile({ media }: { media: MediaAttachment }) {
  const audio = useAudio(media);
  const time = audioClock(audio);
  return (
    <div className={cn(CARD, TILE, "flex flex-col bg-card p-2 hover:border-border")}>
      {audio.element}
      <span
        dir="auto"
        title={media.name}
        className="line-clamp-2 break-words text-xs leading-tight text-muted-foreground"
      >
        {media.name}
      </span>
      <span className="flex flex-1 items-center justify-center">
        <AudioButton media={media} audio={audio} />
      </span>
      {time && <span className={cn(BADGE, "bg-muted")}>{time}</span>}
    </div>
  );
}

/**
 * Audio in a sent message, in the file chip's language: the tinted play box,
 * the name over a thin seek line, and the played time against the length.
 */
function AudioCard({ media }: { media: MediaAttachment }) {
  const audio = useAudio(media);
  const known = Number.isFinite(audio.duration);
  const progress = known && audio.duration > 0 ? Math.min(1, audio.time / audio.duration) : 0;
  return (
    <div
      data-audio-card=""
      className="flex h-12 w-64 max-w-full items-center gap-2 rounded-lg bg-muted ps-1.5 pe-3 text-muted-foreground"
    >
      {audio.element}
      <AudioButton media={media} audio={audio} tone="chip" />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span
          dir="auto"
          title={media.name}
          className="truncate text-sm leading-tight text-foreground"
        >
          {media.name}
        </span>
        <div className="flex items-center gap-2">
          <span
            aria-hidden
            className="-my-1.5 flex flex-1 cursor-pointer items-center py-1.5"
            onPointerDown={(e) => audio.seek(fractionAt(e))}
          >
            <span className="relative h-0.5 w-full overflow-hidden rounded-full bg-foreground/10">
              <span
                className="absolute inset-y-0 start-0 rounded-full bg-foreground/60"
                style={{ width: `${progress * 100}%` }}
              />
            </span>
          </span>
          <span data-audio-time="" className="shrink-0 text-[11px] tabular-nums">
            {known ? `${clock(audio.time)} / ${clock(audio.duration)}` : clock(audio.time)}
          </span>
        </div>
      </div>
    </div>
  );
}

/** A media file that did not load: a tile in the danger tint, never a broken image icon. */
function FailedTile({ media, className }: { media: MediaAttachment; className?: string }) {
  const Warning = React.useContext(MediaIconsContext).failed;
  return (
    <span
      role="img"
      aria-label={msg(
        media.kind === "video"
          ? "shared.agent.media_failed_video"
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
type OpenHandler = (e: React.MouseEvent<HTMLElement>) => void;

/** One media file in the composer row: a 120px tile with the remove X. */
function DraftTile({
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
          className={cn(CARD, TILE, "flex cursor-pointer")}
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

/**
 * One media file in a sent message, in its own shape. One image or video
 * alone may stand 320px tall; several share the line at up to 240 by 160px.
 * A file that does not load shows the failed tile.
 */
function SentItem({
  media,
  alone,
  onOpen,
}: {
  media: MediaAttachment;
  alone: boolean;
  onOpen: OpenHandler;
}) {
  const [broken, setBroken] = React.useState(false);
  if (media.kind === "audio")
    return (
      <li className="max-w-full">
        <AudioCard media={media} />
      </li>
    );
  if (broken || media.status === "failed")
    return (
      <li>
        <FailedTile media={media} />
      </li>
    );
  const fit = cn(
    "block h-auto w-auto max-w-full",
    alone ? "max-h-80 min-h-[7.5rem] min-w-[7.5rem] object-cover" : "max-h-40",
  );
  return (
    <li className={alone ? "max-w-full" : "max-w-[min(100%,240px,calc(50%-0.25rem))]"}>
      <button
        type="button"
        onClick={onOpen}
        aria-label={msg(
          media.kind === "video" ? "shared.agent.media_play_video" : "shared.agent.media_open",
          { name: media.name },
        )}
        title={media.name}
        className={cn(
          CARD,
          "flex max-w-full rounded-lg",
          media.kind === "image" ? "cursor-zoom-in" : "cursor-pointer",
        )}
      >
        {media.kind === "image" ? (
          <img
            src={media.url}
            alt=""
            draggable={false}
            onError={() => setBroken(true)}
            className={fit}
          />
        ) : (
          <VideoPoster media={media} className={fit} onError={() => setBroken(true)} />
        )}
      </button>
    </li>
  );
}

const VIEWER_IMAGE = "max-h-[70vh] max-w-full select-none rounded-2xl object-contain";
const MIN_ZOOM = 1;
const MAX_ZOOM = 8;

/** A zoomable, pannable image: Ctrl or Cmd with the wheel zooms, a pinch zooms, a drag pans, a double click resets. */
function ZoomImage({ src, alt, onError }: { src: string; alt: string; onError: () => void }) {
  const [view, setView] = React.useState({ zoom: 1, x: 0, y: 0 });
  const [loaded, setLoaded] = React.useState(false);
  const drag = React.useRef<{ x: number; y: number } | null>(null);
  const ref = React.useRef<HTMLDivElement | null>(null);
  // React's onWheel is passive, so preventDefault (no page zoom) needs a native listener.
  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      // Codex's ramp: each wheel unit scales by exp(-delta / 200).
      setView((v) => {
        const zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.zoom * Math.exp(-e.deltaY / 200)));
        return zoom === 1 ? { zoom, x: 0, y: 0 } : { ...v, zoom };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);
  return (
    <div
      ref={ref}
      className={cn(
        "flex size-full items-center justify-center overflow-hidden",
        view.zoom > 1 && "cursor-grab active:cursor-grabbing",
      )}
      onPointerDown={(e) => {
        if (view.zoom === 1) return;
        e.currentTarget.setPointerCapture(e.pointerId);
        drag.current = { x: e.clientX - view.x, y: e.clientY - view.y };
      }}
      onPointerMove={(e) => {
        const start = drag.current;
        if (start) setView((v) => ({ ...v, x: e.clientX - start.x, y: e.clientY - start.y }));
      }}
      onPointerUp={() => (drag.current = null)}
      onDoubleClick={() =>
        setView((v) => (v.zoom === 1 ? { zoom: 2, x: 0, y: 0 } : { zoom: 1, x: 0, y: 0 }))
      }
    >
      <img
        src={src}
        alt={alt}
        draggable={false}
        onLoad={() => setLoaded(true)}
        onError={onError}
        className={cn(
          VIEWER_IMAGE,
          "motion-safe:transition-opacity motion-safe:duration-150",
          !loaded && "opacity-0",
        )}
        style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})` }}
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

const SPEEDS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
// Codex hides the overlay controls after 2.5 seconds without a pointer move, a press or a key.
const CONTROLS_IDLE_MS = 2500;
const SEEK_STEP_S = 5;

/**
 * Codex's video player: a click or Space plays, a center play button shows while
 * paused, and a bottom bar has play, the timeline, the time, volume, speed and
 * fullscreen. The bar hides while the clip plays and the pointer rests.
 */
function VideoPlayer({ src, name, onError }: { src: string; name: string; onError: () => void }) {
  const icons = React.useContext(MediaIconsContext);
  const box = React.useRef<HTMLDivElement | null>(null);
  const ref = React.useRef<HTMLVideoElement | null>(null);
  const grab = React.useRef<{ wasPlaying: boolean } | null>(null);
  const [playing, setPlaying] = React.useState(false);
  const [time, setTime] = React.useState(0);
  const [duration, setDuration] = React.useState(0);
  const [volume, setVolume] = React.useState(1);
  const [muted, setMuted] = React.useState(false);
  const [rate, setRate] = React.useState(1);
  const [menu, setMenu] = React.useState<"volume" | "speed" | null>(null);
  const [idle, setIdle] = React.useState(false);
  const [full, setFull] = React.useState(false);

  React.useEffect(() => {
    const el = box.current;
    if (!el || !playing || menu) return;
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
  }, [playing, menu]);

  React.useEffect(() => {
    const onChange = () => {
      const on = document.fullscreenElement === box.current;
      setFull(on);
      if (on) ref.current?.focus();
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const toggle = () => {
    const el = ref.current;
    if (!el) return;
    setMenu(null);
    if (el.paused) void el.play().catch(() => undefined);
    else el.pause();
  };
  const seekTo = (t: number) => {
    const el = ref.current;
    if (!el || !(duration > 0)) return;
    el.currentTime = Math.max(0, Math.min(duration, t));
    setTime(el.currentTime);
  };
  const toggleFull = () => {
    const el = box.current;
    if (!el) return;
    void (
      document.fullscreenElement === el ? document.exitFullscreen() : el.requestFullscreen()
    ).catch(() => undefined);
  };
  const progress = `${clock(time)} / ${clock(duration)}`;
  const Icon = playing ? icons.pause : icons.play;
  const Volume = muted || volume === 0 ? SpeakerSlash : SpeakerHigh;
  const Full = full ? ArrowsIn : ArrowsOut;
  const playLabel = msg(playing ? "shared.agent.media_pause" : "shared.agent.media_play", { name });
  const tool =
    "flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-md text-white/80 transition-colors hover:bg-white/10 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60";
  const hidden = playing && idle && !menu;
  return (
    <div
      ref={box}
      className={cn(
        "relative flex max-w-full overflow-hidden rounded-2xl bg-black",
        hidden && "cursor-none",
      )}
      onKeyDown={(e) => {
        // Esc leaves fullscreen first, so it does not also close the viewer.
        if (e.key === "Escape" && (menu || document.fullscreenElement === e.currentTarget)) {
          e.preventDefault();
          e.stopPropagation();
          if (menu) setMenu(null);
          else toggleFull();
        }
      }}
    >
      <video
        ref={ref}
        src={src}
        autoPlay
        playsInline
        tabIndex={0}
        aria-label={name}
        onError={onError}
        className={cn(
          "block max-w-full cursor-pointer object-contain focus-visible:outline-none",
          full ? "size-full" : "max-h-[70vh]",
        )}
        onClick={toggle}
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
          setPlaying(true);
        }}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onVolumeChange={(e) => {
          setVolume(e.currentTarget.volume);
          setMuted(e.currentTarget.muted);
        }}
        onRateChange={(e) => setRate(e.currentTarget.playbackRate)}
      />
      {!playing && (
        <button
          type="button"
          onClick={toggle}
          aria-label={playLabel}
          title={playLabel}
          className="absolute left-1/2 top-1/2 flex size-12 -translate-x-1/2 -translate-y-1/2 cursor-pointer items-center justify-center rounded-full bg-black/55 text-white backdrop-blur-sm transition-transform duration-75 active:scale-[0.97] motion-reduce:active:scale-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
        >
          <icons.play className="size-5" aria-hidden />
        </button>
      )}
      <div
        className={cn(
          "absolute inset-x-0 bottom-0 flex items-center gap-1 bg-black/75 px-1.5 text-white transition-opacity duration-150",
          hidden && "pointer-events-none opacity-0",
        )}
      >
        <button
          type="button"
          onClick={toggle}
          aria-label={playLabel}
          title={playLabel}
          className={tool}
        >
          <Icon className="size-4" aria-hidden />
        </button>
        <Scrub
          className="flex-1 px-1"
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
        <span
          dir="ltr"
          className="shrink-0 px-1 text-xs whitespace-nowrap tabular-nums text-white/70"
        >
          {progress}
        </span>
        <div className="relative">
          <button
            type="button"
            onClick={() => setMenu((m) => (m === "volume" ? null : "volume"))}
            aria-label={msg("shared.agent.media_volume")}
            aria-expanded={menu === "volume"}
            title={msg("shared.agent.media_volume")}
            className={tool}
          >
            <Volume className="size-4" aria-hidden />
          </button>
          {menu === "volume" && (
            <div className="absolute end-0 bottom-full mb-2 flex w-40 items-center gap-2 rounded-xl bg-black/85 p-1.5 shadow-[var(--shadow-float)]">
              <button
                type="button"
                onClick={() => ref.current && (ref.current.muted = !ref.current.muted)}
                aria-label={msg("shared.agent.media_mute")}
                aria-pressed={muted}
                className={tool}
              >
                <Volume className="size-4" aria-hidden />
              </button>
              <Scrub
                className="flex-1 pe-1"
                label={msg("shared.agent.media_volume")}
                valueText={`${Math.round((muted ? 0 : volume) * 100)}%`}
                value={muted ? 0 : volume}
                max={1}
                onChange={(v) => {
                  const el = ref.current;
                  if (!el) return;
                  el.volume = v;
                  el.muted = false;
                }}
                onKeyDown={(e) => {
                  if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
                  e.preventDefault();
                  e.stopPropagation();
                  const el = ref.current;
                  if (!el) return;
                  el.volume = Math.max(
                    0,
                    Math.min(1, el.volume + (e.key === "ArrowRight" ? 0.1 : -0.1)),
                  );
                  el.muted = false;
                }}
              />
            </div>
          )}
        </div>
        <div className="relative">
          <button
            type="button"
            onClick={() => setMenu((m) => (m === "speed" ? null : "speed"))}
            aria-label={msg("shared.agent.media_options")}
            aria-expanded={menu === "speed"}
            title={msg("shared.agent.media_options")}
            className={tool}
          >
            <DotsThree className="size-4" aria-hidden />
          </button>
          {menu === "speed" && (
            <div
              role="radiogroup"
              aria-label={msg("shared.agent.media_speed")}
              className="absolute end-0 bottom-full mb-2 flex w-36 flex-col rounded-xl bg-black/85 p-1 shadow-[var(--shadow-float)]"
            >
              <span className="px-2 pt-1 pb-1.5 text-[11px] text-white/60">
                {msg("shared.agent.media_speed")}
              </span>
              {SPEEDS.map((s) => (
                <button
                  key={s}
                  type="button"
                  role="radio"
                  aria-checked={rate === s}
                  onClick={() => {
                    if (ref.current) ref.current.playbackRate = s;
                    setMenu(null);
                  }}
                  className="flex h-7 cursor-pointer items-center justify-between rounded-md px-2 text-xs tabular-nums hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60"
                >
                  {`${s}×`}
                  {rate === s && <Check className="size-3.5" aria-hidden />}
                </button>
              ))}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={toggleFull}
          aria-label={msg(
            full ? "shared.agent.media_exit_fullscreen" : "shared.agent.media_fullscreen",
          )}
          title={msg(full ? "shared.agent.media_exit_fullscreen" : "shared.agent.media_fullscreen")}
          className={tool}
        >
          <Full className="size-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}

/** The open file in the viewer. A file that fails to load says so and offers Try again. */
function CurrentSlide({ media }: { media: MediaAttachment }) {
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

const SLIDE_GAP_PX = 32;
const SLIDE_VW = 75;
const VIEWER_FADE =
  "data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 duration-150 motion-reduce:animate-none";

/**
 * Claude's lightbox: the media floats on a dark backdrop with no card. The
 * slides sit side by side and slide into place. Close and download sit at the
 * top end; previous, "n of total" and next sit at the bottom. The arrow keys,
 * Home and End move between files, mirrored in RTL, and stop at the ends.
 */
export function MediaViewer({
  media,
  index,
  onIndex,
  onClose,
  returnFocus,
}: {
  media: readonly MediaAttachment[];
  index: number | null;
  onIndex: (i: number) => void;
  onClose: () => void;
  /** The control that opened the viewer; it gets the focus back on close. */
  returnFocus?: React.RefObject<HTMLElement | null>;
}) {
  const icons = React.useContext(MediaIconsContext);
  const item = index === null ? null : media[index];
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
      "pointer-events-auto flex rounded-lg p-2 transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/60",
      enabled ? "cursor-pointer text-white hover:bg-white/10" : "cursor-default text-white/30",
    );
  return (
    <DialogPrimitive.Root open={item !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className={cn("fixed inset-0 z-50 bg-black/80", VIEWER_FADE)} />
        <DialogPrimitive.Content
          className={cn(
            "fixed inset-0 z-50 flex items-center overflow-hidden outline-none",
            VIEWER_FADE,
            "data-[state=open]:zoom-in-95 data-[state=closed]:zoom-out-95",
          )}
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
            if (index === null) return;
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
                  item.kind === "video"
                    ? "shared.agent.media_preview_video"
                    : "shared.agent.media_preview_image",
                )}
              </DialogPrimitive.Title>
              <DialogPrimitive.Description className="sr-only">
                {item.name}
              </DialogPrimitive.Description>
              <div
                data-slide=""
                className="flex h-full shrink-0 items-center gap-8 [transform:translateX(var(--slide))] motion-safe:transition-transform motion-safe:duration-300 motion-safe:ease-[var(--ease-snappy)] rtl:[transform:translateX(calc(-1*var(--slide)))]"
                style={
                  {
                    "--slide": `calc(${(100 - SLIDE_VW) / 2}vw - ${index * SLIDE_VW}vw - ${index * SLIDE_GAP_PX}px)`,
                  } as React.CSSProperties
                }
              >
                {media.map((m, i) => (
                  <div
                    key={m.id}
                    data-slide=""
                    inert={i !== index}
                    className={cn(
                      "flex h-full w-[75vw] shrink-0 items-center justify-center motion-safe:transition-opacity motion-safe:duration-150",
                      i !== index && "opacity-30",
                    )}
                  >
                    {/* Only the open file and its neighbours load. */}
                    {Math.abs(i - index) <= 1 &&
                      (i === index ? (
                        <CurrentSlide key={m.id} media={m} />
                      ) : m.kind === "image" ? (
                        <img src={m.url} alt="" draggable={false} className={VIEWER_IMAGE} />
                      ) : (
                        <video
                          src={`${m.url}#t=0.1`}
                          muted
                          preload="metadata"
                          className="max-h-[70vh] max-w-full rounded-2xl"
                        />
                      ))}
                  </div>
                ))}
              </div>
              <div className="pointer-events-none absolute inset-x-0 top-0 z-20 flex items-center justify-end gap-1 bg-linear-to-b from-black/80 to-transparent p-4">
                <a
                  href={item.url}
                  download={item.name}
                  aria-label={msg("shared.agent.media_download_name", { name: item.name })}
                  title={msg("shared.agent.media_download")}
                  className={tool}
                >
                  <Download className="size-4" aria-hidden />
                </a>
                <DialogPrimitive.Close
                  aria-label={msg("shared.dialog.close")}
                  title={msg("shared.dialog.close")}
                  className={tool}
                >
                  <Close className="size-4" aria-hidden />
                </DialogPrimitive.Close>
              </div>
              {media.length > 1 && (
                <>
                  <div
                    aria-hidden
                    className="pointer-events-none absolute inset-x-0 bottom-0 h-[max(128px,10vh)]"
                    style={{
                      background:
                        "linear-gradient(180deg, transparent 0%, rgb(0 0 0 / 0.8) 66.41%)",
                    }}
                  />
                  <div className="pointer-events-none absolute inset-x-0 bottom-6 z-10 flex h-14 items-center justify-center px-6">
                    <button
                      type="button"
                      onClick={() => go(index - 1)}
                      aria-disabled={index === 0 || undefined}
                      aria-label={msg("shared.agent.media_previous")}
                      title={msg("shared.agent.media_previous")}
                      className={arrow(index > 0)}
                    >
                      <Previous className="size-5 rtl:-scale-x-100" aria-hidden />
                    </button>
                    <span
                      role="status"
                      className="select-none px-2 text-sm tabular-nums text-white/80"
                    >
                      {msg("shared.agent.media_position", {
                        index: index + 1,
                        count: media.length,
                      })}
                    </span>
                    <button
                      type="button"
                      onClick={() => go(index + 1)}
                      aria-disabled={index === last || undefined}
                      aria-label={msg("shared.agent.media_next")}
                      title={msg("shared.agent.media_next")}
                      className={arrow(index < last)}
                    >
                      <Next className="size-5 rtl:-scale-x-100" aria-hidden />
                    </button>
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
 * The media of one message. In the composer it is one scrolling row of 120px
 * tiles; in a sent message it is a wrapping list of cards in their own shape.
 * Images and videos open one shared viewer; audio plays in place. Renders
 * nothing when the list is empty.
 */
export function MediaTiles({
  media: given,
  onRemove,
  size = "draft",
  className,
}: {
  media: readonly MediaAttachment[];
  onRemove?: (id: string) => void;
  size?: "draft" | "sent";
  className?: string;
}) {
  const [open, setOpen] = React.useState<number | null>(null);
  const media = useResolvedMedia(given);
  const opener = React.useRef<HTMLElement | null>(null);
  // A sent file marked failed has no URL to show. A draft file that failed to
  // upload still plays from its local URL, so it stays in the viewer.
  const visual = React.useMemo(
    () => media.filter((m) => m.kind !== "audio" && !(size === "sent" && m.status === "failed")),
    [media, size],
  );
  if (!media.length) return null;
  const openOf =
    (m: MediaAttachment): OpenHandler =>
    (e) => {
      opener.current = e.currentTarget;
      setOpen(visual.indexOf(m));
    };
  const viewer = (
    <MediaViewer
      media={visual}
      index={open === null || !visual.length ? null : Math.min(open, visual.length - 1)}
      onIndex={setOpen}
      onClose={() => setOpen(null)}
      returnFocus={opener}
    />
  );
  if (size === "draft")
    return (
      <>
        <TileRow className={className}>
          {media.map((m) => (
            <DraftTile
              key={m.id}
              media={m}
              onOpen={openOf(m)}
              onRemove={onRemove && (() => onRemove(m.id))}
            />
          ))}
        </TileRow>
        {viewer}
      </>
    );
  return (
    <>
      <ul
        role="list"
        aria-label={msg("shared.agent.media_list")}
        className={cn("flex max-w-full flex-wrap items-end justify-end gap-2 p-px", className)}
      >
        {media.map((m) => (
          <SentItem key={m.id} media={m} alone={visual.length === 1} onOpen={openOf(m)} />
        ))}
      </ul>
      {viewer}
    </>
  );
}
