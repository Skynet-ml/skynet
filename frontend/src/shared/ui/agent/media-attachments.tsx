"use client";

import * as React from "react";
import {
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
  X,
} from "@/shared/ui/icons";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { Dialog, DialogContent, DialogTitle } from "@/shared/ui/primitives/dialog";

export type MediaKind = "image" | "video" | "audio";

/** An image, video or audio file that goes with a message and renders as itself, not as text. */
export interface MediaAttachment {
  id: string;
  name: string;
  kind: MediaKind;
  /** Where the file plays from: an object URL in the draft, a stored URL once sent. */
  url: string;
}

/** The media kind of a file, or null when the file is not image, video or audio. */
export function mediaKind(file: { type: string }): MediaKind | null {
  const top = file.type.split("/")[0];
  return top === "image" || top === "video" || top === "audio" ? top : null;
}

/** A local attachment for a picked, pasted or dropped file. The host frees its URL. */
export function mediaFromFile(file: File): MediaAttachment | null {
  const kind = mediaKind(file);
  if (!kind) return null;
  // A pasted screenshot has a generic name ("image.png"), which is still the best label.
  return { id: crypto.randomUUID(), name: file.name || kind, kind, url: URL.createObjectURL(file) };
}

type IconSlot = "remove" | "play" | "pause" | "previous" | "next" | "download" | "close";
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
});

type Size = "draft" | "sent";

/** Seconds as m:ss; 0:00 until the length is known. */
function clock(seconds: number): string {
  if (!Number.isFinite(seconds)) return "0:00";
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

/** Where along an element a pointer sits, from the reading start: 0..1, mirrored in RTL. */
function fractionAt(e: React.PointerEvent<Element>): number {
  const box = e.currentTarget.getBoundingClientRect();
  const f = (e.clientX - box.left) / box.width;
  return Math.min(1, Math.max(0, getComputedStyle(e.currentTarget).direction === "rtl" ? 1 - f : f));
}

const TILE = cn(
  "relative cursor-pointer overflow-hidden rounded-lg border border-border bg-muted/60",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 focus-visible:ring-inset",
  // The hover tint sits above the picture, as Codex's bg-text/5 overlay does.
  "after:pointer-events-none after:absolute after:inset-0 after:rounded-[inherit] after:transition-colors hover:after:bg-foreground/5",
);

/** The hover X in the tile's corner; touch screens always show it. */
function RemoveButton({ name, onRemove }: { name: string; onRemove: () => void }) {
  const Remove = React.useContext(MediaIconsContext).remove;
  return (
    <button
      type="button"
      onClick={onRemove}
      aria-label={msg("shared.agent.media_remove", { name })}
      className={cn(
        "absolute -top-0.5 -end-0.5 z-10 flex size-4 cursor-pointer items-center justify-center rounded-full bg-foreground text-background shadow-sm",
        "opacity-0 transition-opacity group-hover/media:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100",
      )}
    >
      <Remove className="size-2.5" aria-hidden />
    </button>
  );
}

/** A video's length and width-to-height ratio, read from its metadata. */
function useVideoShape(url: string): { duration: number; ratio: number } {
  const [shape, setShape] = React.useState({ duration: NaN, ratio: 16 / 9 });
  React.useEffect(() => {
    const el = document.createElement("video");
    el.preload = "metadata";
    el.onloadedmetadata = () =>
      setShape({ duration: el.duration, ratio: el.videoHeight ? el.videoWidth / el.videoHeight : 16 / 9 });
    el.src = url;
    return () => {
      el.onloadedmetadata = null;
      el.removeAttribute("src");
    };
  }, [url]);
  return shape;
}

/** Video as its first frame in the clip's own shape, with a play mark and the length. A click opens the viewer. */
function VideoTile({ media, size, onOpen }: { media: MediaAttachment; size: Size; onOpen: () => void }) {
  const PlayIcon = React.useContext(MediaIconsContext).play;
  const { duration, ratio } = useVideoShape(media.url);
  const height = size === "draft" ? 54 : 96;
  // The ratio is clamped, so a phone clip stays tall and a wide clip stays short.
  // 44px is the least width that still fits the length pill.
  const width = Math.max(44, Math.round(height * Math.min(16 / 9, Math.max(9 / 16, ratio))));
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label={msg("shared.agent.media_open", { name: media.name })}
      title={media.name}
      className={cn(TILE, "flex items-center justify-center")}
      style={{ width, height }}
    >
      {/* #t=0.1 asks the browser for the first frame as the poster. */}
      <video src={`${media.url}#t=0.1`} muted preload="metadata" className="size-full object-cover" />
      <PlayIcon className="absolute size-4 text-white drop-shadow" aria-hidden />
      <span className="absolute bottom-1 end-1 rounded-full bg-black/60 px-1.5 py-px text-[10px] font-medium tabular-nums text-white">
        {clock(duration)}
      </span>
    </button>
  );
}

const peaksCache = new Map<string, Promise<number[]>>();

/** Loudness of an audio file in `count` slices, scaled to 0..1, or null while it decodes. */
function usePeaks(url: string, count: number): number[] | null {
  const [peaks, setPeaks] = React.useState<number[] | null>(null);
  React.useEffect(() => {
    const key = `${url}#${count}`;
    let job = peaksCache.get(key);
    if (!job) {
      job = fetch(url)
        .then((r) => r.arrayBuffer())
        .then(async (buffer) => {
          const context = new AudioContext();
          try {
            return await context.decodeAudioData(buffer);
          } finally {
            void context.close();
          }
        })
        .then((audio) => {
          const data = audio.getChannelData(0);
          const step = Math.max(1, Math.floor(data.length / count));
          // Every 16th sample is enough for a slice's peak and keeps long files fast.
          const out = Array.from({ length: count }, (_, i) => {
            let max = 0;
            for (let j = i * step; j < (i + 1) * step; j += 16) max = Math.max(max, Math.abs(data[j] ?? 0));
            return max;
          });
          const top = Math.max(...out, 0.001);
          return out.map((v) => Math.max(0.08, v / top));
        });
      peaksCache.set(key, job);
    }
    let live = true;
    job.then(
      (p) => live && setPeaks(p),
      () => live && setPeaks(Array<number>(count).fill(0.3)),
    );
    return () => {
      live = false;
    };
  }, [url, count]);
  return peaks;
}

const WAVE_POINTS = 96;
const WAVE_HEIGHT = 22;

/**
 * Audio as one thin ink line of its loudness, with play in place. The line
 * darkens as the clip plays, and a click on the line seeks.
 */
function AudioTile({ media, size }: { media: MediaAttachment; size: Size }) {
  const icons = React.useContext(MediaIconsContext);
  const ref = React.useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = React.useState(false);
  const [time, setTime] = React.useState(0);
  const [duration, setDuration] = React.useState(NaN);
  // Codex swaps play for a download link when the browser cannot play the file.
  const [broken, setBroken] = React.useState(false);
  const peaks = usePeaks(media.url, WAVE_POINTS);
  const clipId = `played-${React.useId().replace(/[^\w-]/g, "")}`;
  const width = size === "draft" ? 150 : 220;
  const progress = duration > 0 ? time / duration : 0;
  // Each point swings above or below the middle in turn, so the line reads as a waveform.
  const path = (peaks ?? Array<number>(WAVE_POINTS).fill(0.1))
    .map((v, i) => {
      const x = ((i / (WAVE_POINTS - 1)) * width).toFixed(1);
      const y = (WAVE_HEIGHT / 2 + v * WAVE_HEIGHT * 0.45 * (i % 2 ? 1 : -1)).toFixed(1);
      return `${i ? "L" : "M"}${x} ${y}`;
    })
    .join(" ");
  const seek = (fraction: number) => {
    const el = ref.current;
    if (el && Number.isFinite(el.duration)) el.currentTime = fraction * el.duration;
  };
  const toggle = () => {
    const el = ref.current;
    if (!el) return;
    if (el.paused) void el.play().catch(() => setBroken(true));
    else el.pause();
  };
  React.useEffect(() => {
    const el = ref.current;
    return () => el?.pause();
  }, []);
  const Icon = playing ? icons.pause : icons.play;
  const label = msg(playing ? "shared.agent.media_pause" : "shared.agent.media_play", { name: media.name });
  const round =
    "flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-full bg-foreground text-background transition-transform active:scale-95";
  return (
    <div
      className={cn(
        "relative flex items-center gap-2.5 rounded-lg border border-border bg-card px-2",
        size === "draft" ? "h-[54px]" : "h-16",
      )}
    >
      <audio
        ref={ref}
        src={media.url}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
        onLoadedMetadata={(e) => setDuration(e.currentTarget.duration)}
        // At the end Codex goes back to the start, so the tile shows the full length again.
        onEnded={(e) => {
          e.currentTarget.currentTime = 0;
          setTime(0);
        }}
        onError={() => setBroken(true)}
      />
      {broken ? (
        <a
          href={media.url}
          download={media.name}
          aria-label={msg("shared.agent.media_download_name", { name: media.name })}
          title={msg("shared.agent.media_download_name", { name: media.name })}
          className={round}
        >
          <icons.download className="size-3.5" aria-hidden />
        </a>
      ) : (
        <button type="button" onClick={toggle} aria-label={label} title={label} className={round}>
          <Icon className="size-3.5" aria-hidden />
        </button>
      )}
      <div className="flex flex-col gap-0.5">
        <svg
          width={width}
          height={WAVE_HEIGHT}
          viewBox={`0 0 ${width} ${WAVE_HEIGHT}`}
          aria-hidden
          // Mirrored in RTL, so the line plays from the reading start, as fractionAt reads it.
          className="cursor-pointer rtl:-scale-x-100"
          onPointerDown={(e) => seek(fractionAt(e))}
        >
          <defs>
            <clipPath id={clipId}>
              <rect width={width * progress} height={WAVE_HEIGHT} />
            </clipPath>
          </defs>
          <path d={path} fill="none" stroke="currentColor" strokeWidth="1" strokeLinejoin="round" className="text-foreground/25" />
          <path d={path} fill="none" stroke="currentColor" strokeWidth="1" strokeLinejoin="round" className="text-foreground" clipPath={`url(#${clipId})`} />
        </svg>
        <div className="flex justify-between gap-2 text-[10px] text-muted-foreground" style={{ width }}>
          <span className="truncate" dir="auto">
            {media.name}
          </span>
          <span className="shrink-0 tabular-nums">{clock(playing || time ? time : duration)}</span>
        </div>
      </div>
    </div>
  );
}

/**
 * One attachment as a tile. Images use Codex's sizes: 54px in the composer and
 * 80px in the sent message. Video keeps its own shape; audio plays in place.
 * A hover X removes it.
 */
function MediaTile({
  media,
  onOpen,
  onRemove,
  size,
}: {
  media: MediaAttachment;
  onOpen: () => void;
  onRemove?: () => void;
  size: Size;
}) {
  return (
    <div className="group/media relative shrink-0">
      {media.kind === "image" && (
        <button
          type="button"
          onClick={onOpen}
          aria-label={msg("shared.agent.media_open", { name: media.name })}
          title={media.name}
          className={cn(TILE, "flex items-center justify-center", size === "draft" ? "size-[54px]" : "size-20")}
        >
          {/* A plain img: object URLs and stored files skip the image optimizer. */}
          <img src={media.url} alt="" className="size-full rounded-md object-cover" draggable={false} />
        </button>
      )}
      {media.kind === "video" && <VideoTile media={media} size={size} onOpen={onOpen} />}
      {media.kind === "audio" && <AudioTile media={media} size={size} />}
      {onRemove && <RemoveButton name={media.name} onRemove={onRemove} />}
    </div>
  );
}

const MIN_ZOOM = 1;
const MAX_ZOOM = 8;

/** A zoomable, pannable image: Ctrl or Cmd with the wheel zooms, a pinch zooms, a drag pans, a double click resets. */
function ZoomImage({ src, alt }: { src: string; alt: string }) {
  const [view, setView] = React.useState({ zoom: 1, x: 0, y: 0 });
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
      className={cn("flex min-h-0 flex-1 items-center justify-center overflow-hidden", view.zoom > 1 && "cursor-grab active:cursor-grabbing")}
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
      onDoubleClick={() => setView((v) => (v.zoom === 1 ? { zoom: 2, x: 0, y: 0 } : { zoom: 1, x: 0, y: 0 }))}
    >
      <img
        src={src}
        alt={alt}
        draggable={false}
        className="max-h-[75dvh] max-w-full select-none rounded-lg object-contain"
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
        <span className="absolute inset-y-0 start-0 rounded-full bg-white" style={{ width: `${fill}%` }} />
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
function VideoPlayer({ src, name }: { src: string; name: string }) {
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
      for (const type of ["pointermove", "pointerdown", "keydown"]) el.removeEventListener(type, wake);
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
    void (document.fullscreenElement === el ? document.exitFullscreen() : el.requestFullscreen()).catch(() => undefined);
  };
  const progress = `${clock(time)} / ${clock(duration)}`;
  const Icon = playing ? icons.pause : icons.play;
  const Volume = muted || volume === 0 ? SpeakerSlash : SpeakerHigh;
  const Full = full ? ArrowsIn : ArrowsOut;
  const playLabel = msg(playing ? "shared.agent.media_pause" : "shared.agent.media_play", { name });
  const tool =
    "flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-md text-white/80 transition-colors hover:bg-white/10 hover:text-white";
  const hidden = playing && idle && !menu;
  return (
    <div
      ref={box}
      className={cn("relative flex w-full overflow-hidden rounded-lg bg-black", hidden && "cursor-none")}
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
        className={cn("block w-full cursor-pointer object-contain focus-visible:outline-none", full ? "h-full" : "max-h-[75dvh]")}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key !== " " && e.key !== "Enter") return;
          e.preventDefault();
          if (!e.repeat) toggle();
        }}
        onDurationChange={(e) => setDuration(Number.isFinite(e.currentTarget.duration) ? e.currentTarget.duration : 0)}
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
          className="absolute left-1/2 top-1/2 flex size-12 -translate-x-1/2 -translate-y-1/2 cursor-pointer items-center justify-center rounded-full bg-black/55 text-white backdrop-blur-sm transition-transform active:scale-95"
        >
          <icons.play className="size-5" aria-hidden />
        </button>
      )}
      <div
        className={cn(
          "absolute inset-x-0 bottom-0 flex items-center gap-1 bg-black/75 px-1.5 text-white transition-opacity duration-200",
          hidden && "pointer-events-none opacity-0",
        )}
      >
        <button type="button" onClick={toggle} aria-label={playLabel} title={playLabel} className={tool}>
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
            const forward = (e.key === "ArrowRight") !== (getComputedStyle(e.currentTarget).direction === "rtl");
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
        <span dir="ltr" className="shrink-0 px-1 text-xs whitespace-nowrap tabular-nums text-white/70">{progress}</span>
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
            <div className="absolute end-0 bottom-full mb-2 flex w-40 items-center gap-2 rounded-lg bg-black/85 p-1.5 shadow-lg">
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
                  el.volume = Math.max(0, Math.min(1, el.volume + (e.key === "ArrowRight" ? 0.1 : -0.1)));
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
              className="absolute end-0 bottom-full mb-2 flex w-36 flex-col rounded-lg bg-black/85 p-1 shadow-lg"
            >
              <span className="px-2 pt-1 pb-1.5 text-[11px] text-white/60">{msg("shared.agent.media_speed")}</span>
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
                  className="flex h-7 cursor-pointer items-center justify-between rounded-md px-2 text-xs tabular-nums hover:bg-white/10"
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
          aria-label={msg(full ? "shared.agent.media_exit_fullscreen" : "shared.agent.media_fullscreen")}
          title={msg(full ? "shared.agent.media_exit_fullscreen" : "shared.agent.media_fullscreen")}
          className={tool}
        >
          <Full className="size-4" aria-hidden />
        </button>
      </div>
    </div>
  );
}

/** The full-size viewer: one attachment at a time, with previous and next, download and close. */
export function MediaViewer({
  media,
  index,
  onIndex,
  onClose,
  onRemove,
}: {
  media: readonly MediaAttachment[];
  index: number | null;
  onIndex: (i: number) => void;
  onClose: () => void;
  onRemove?: (id: string) => void;
}) {
  const icons = React.useContext(MediaIconsContext);
  const item = index === null ? null : media[index];
  const many = media.length > 1;
  const step = (by: number) => index !== null && onIndex((index + by + media.length) % media.length);
  const Previous = icons.previous;
  const Next = icons.next;
  const Download = icons.download;
  const Close = icons.close;
  const tool =
    "flex size-8 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground";
  return (
    <Dialog open={item !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        showCloseButton={false}
        onKeyDown={(e) => {
          // The arrows follow reading order, so in RTL the right arrow goes back.
          if (!many || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
          const rtl = getComputedStyle(e.currentTarget).direction === "rtl";
          step((e.key === "ArrowRight") !== rtl ? 1 : -1);
        }}
        className={cn(
          "flex flex-col gap-2 p-2",
          item?.kind === "audio" ? "sm:max-w-[min(28rem,92vw)]" : "sm:max-w-[min(64rem,92vw)]",
        )}
      >
        {item && (
          <>
            <div className="flex items-center gap-1 ps-2">
              <DialogTitle className="min-w-0 flex-1 truncate text-sm font-medium" dir="auto">
                {item.name}
              </DialogTitle>
              {many && (
                <span className="shrink-0 px-1 text-xs tabular-nums text-muted-foreground">
                  {msg("shared.agent.media_position", { index: index! + 1, count: media.length })}
                </span>
              )}
              <a href={item.url} download={item.name} aria-label={msg("shared.agent.media_download")} className={tool}>
                <Download className="size-4" aria-hidden />
              </a>
              {/* Tiles past "+N" have no hover X, so a draft removes them here. */}
              {onRemove && (
                <button
                  type="button"
                  onClick={() => onRemove(item.id)}
                  aria-label={msg("shared.agent.media_remove", { name: item.name })}
                  className={cn(tool, "w-auto px-2 text-xs")}
                >
                  {msg("shared.agent.media_remove_short")}
                </button>
              )}
              <button type="button" onClick={onClose} aria-label={msg("shared.agent.cancel")} className={tool}>
                <Close className="size-4" aria-hidden />
              </button>
            </div>
            <div className="relative flex min-h-0 items-center justify-center">
              {item.kind === "image" && <ZoomImage key={item.id} src={item.url} alt={item.name} />}
              {item.kind === "video" && <VideoPlayer key={item.id} src={item.url} name={item.name} />}
              {item.kind === "audio" && <audio key={item.id} src={item.url} controls autoPlay className="w-full" />}
              {many && (
                <>
                  <button
                    type="button"
                    onClick={() => step(-1)}
                    aria-label={msg("shared.agent.media_previous")}
                    className={cn(tool, "absolute start-1 top-1/2 -translate-y-1/2 bg-background/80 shadow-sm")}
                  >
                    <Previous className="size-4 rtl:-scale-x-100" aria-hidden />
                  </button>
                  <button
                    type="button"
                    onClick={() => step(1)}
                    aria-label={msg("shared.agent.media_next")}
                    className={cn(tool, "absolute end-1 top-1/2 -translate-y-1/2 bg-background/80 shadow-sm")}
                  >
                    <Next className="size-4 rtl:-scale-x-100" aria-hidden />
                  </button>
                </>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

const MAX_TILES = 5;

/** A row of media tiles that open one shared viewer; renders nothing when the list is empty. */
export function MediaTiles({
  media,
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
  if (!media.length) return null;
  // Past MAX_TILES, the last tile becomes "+N" and opens the viewer, which pages through the rest.
  const shown = media.length > MAX_TILES ? MAX_TILES - 1 : media.length;
  const hidden = media.length - shown;
  return (
    <>
      <div className={cn("flex flex-wrap gap-2", className)}>
        {media.slice(0, shown).map((m, i) => (
          <MediaTile
            key={m.id}
            media={m}
            size={size}
            onOpen={() => setOpen(i)}
            onRemove={onRemove && (() => onRemove(m.id))}
          />
        ))}
        {hidden > 0 && (
          <button
            type="button"
            onClick={() => setOpen(shown)}
            aria-label={msg("shared.agent.media_more_label", { count: hidden })}
            title={media.slice(shown).map((m) => m.name).join("\n")}
            className={cn(
              TILE,
              "flex shrink-0 items-center justify-center font-medium tabular-nums text-muted-foreground",
              size === "draft" ? "size-[54px] text-[13px]" : "size-20 text-[15px]",
            )}
          >
            {msg("shared.agent.media_more", { count: hidden })}
          </button>
        )}
      </div>
      <MediaViewer
        media={media}
        index={open === null ? null : Math.min(open, media.length - 1)}
        onIndex={setOpen}
        onClose={() => setOpen(null)}
        onRemove={onRemove && ((id) => (media.length === 1 ? (setOpen(null), onRemove(id)) : onRemove(id)))}
      />
    </>
  );
}
