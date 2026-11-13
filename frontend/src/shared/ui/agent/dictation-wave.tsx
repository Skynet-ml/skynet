"use client";

import * as React from "react";

import { cn } from "@/shared/lib/utils";

import { HEAR_FLOOR, WAVE_STEP_MS, frameLevel, pushBar, slotX } from "./dictation-level";

const BAR = 2;
const GAP = 2;
const MIN_BAR = 2;
// The sweep that shows the take is with the transcriber, in ms per pass.
const SWEEP_MS = 1400;
// The newest bars take the accent color and a soft glow, so the live edge reads
// as the voice right now.
const ACCENT_BARS = 4;
// With reduced motion the strip shows one level in place, so it changes slowly.
const REDUCED_STEP_MS = 250;

type AudioContextCtor = typeof AudioContext;

/** The browser's AudioContext, or null where Web Audio is missing. */
function audioContextCtor(): AudioContextCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    AudioContext?: AudioContextCtor;
    webkitAudioContext?: AudioContextCtor;
  };
  return w.AudioContext ?? w.webkitAudioContext ?? null;
}

/**
 * A live waveform of the dictation take, drawn on a canvas so the mic level
 * never re-renders the composer. While `stream` is live, new bars enter at
 * the inline end and older ones move toward the start; the newest bars glow in
 * the accent color, and empty slots show as a faint baseline. When `processing`
 * is set, the take freezes and a soft highlight sweeps across it while the
 * transcriber works.
 */
export function DictationWave({
  stream,
  processing = false,
  onLevel,
  className,
}: {
  /** The mic stream to listen to; null keeps the bars as they are. */
  stream: MediaStream | null;
  /** True while the take is with the transcriber. */
  processing?: boolean;
  /** Receives the level of each mic frame read, from 0 (silence) to 1. */
  onLevel?: (level: number) => void;
  className?: string;
}) {
  const canvasRef = React.useRef<HTMLCanvasElement | null>(null);
  // A ref, so a new callback does not restart the audio graph.
  const onLevelRef = React.useRef(onLevel);
  React.useEffect(() => {
    onLevelRef.current = onLevel;
  }, [onLevel]);
  const barsRef = React.useRef<number[]>([]);

  React.useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

    let analyser: AnalyserNode | null = null;
    let audio: AudioContext | null = null;
    const Ctor = audioContextCtor();
    if (stream && Ctor) {
      try {
        audio = new Ctor();
        // WebKit can create the context suspended, and a suspended analyser reads silence.
        if (audio.state === "suspended") void audio.resume().catch(() => undefined);
        analyser = audio.createAnalyser();
        analyser.fftSize = 1024;
        audio.createMediaStreamSource(stream).connect(analyser);
      } catch {
        analyser = null;
      }
    }
    const frame = new Uint8Array(analyser?.fftSize ?? 0);

    let width = 0;
    let height = 0;
    let rtl = false;
    let ink = "";
    let accent = "";
    let peak = 0;
    let lastStep = performance.now();
    let lastColors = -Infinity;
    let raf = 0;
    const style = getComputedStyle(canvas);
    // The strip fades its text color when the take goes to the transcriber, so
    // the colors are read again on each step, not only once at the start.
    const readColors = (now: number) => {
      lastColors = now;
      ink = style.color;
      accent = style.getPropertyValue("--primary").trim() || ink;
    };
    const draw = (now: number) => {
      if (now - lastColors >= WAVE_STEP_MS) readColors(now);
      const slots = Math.max(1, Math.floor((width + GAP) / (BAR + GAP)));
      if (analyser) {
        analyser.getByteTimeDomainData(frame);
        const level = frameLevel(frame);
        // A context that does not run reads flat silence, so its level is not reported.
        if (audio?.state === "running") onLevelRef.current?.(frameLevel(frame, HEAR_FLOOR));
        peak = Math.max(peak, level);
        if (now - lastStep >= (reduced ? REDUCED_STEP_MS : WAVE_STEP_MS)) {
          barsRef.current = reduced
            ? new Array<number>(slots).fill(peak)
            : pushBar(barsRef.current, peak, slots);
          peak = 0;
          lastStep = now;
        }
      }
      const bars = barsRef.current.slice(-slots);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = ink;
      const mid = height / 2;
      const empty = slots - bars.length;
      const sweep = processing && !reduced ? (now % SWEEP_MS) / SWEEP_MS : -1;
      for (let i = 0; i < slots; i++) {
        // The newest bar sits at the inline end.
        const x = slotX(i, width, BAR, GAP, rtl);
        const bar = i - empty;
        if (bar < 0) {
          ctx.fillStyle = ink;
          ctx.shadowBlur = 0;
          ctx.globalAlpha = 0.18;
          ctx.fillRect(x, mid - 0.5, BAR, 1);
          continue;
        }
        const h = Math.max(MIN_BAR, (bars[bar] ?? 0) * (height - 2));
        let alpha = 0.85;
        const live = !processing && bars.length - bar <= ACCENT_BARS;
        ctx.fillStyle = live ? accent : ink;
        ctx.shadowColor = accent;
        ctx.shadowBlur = live ? 6 : 0;
        if (processing) {
          alpha = 0.3;
          if (sweep >= 0) {
            const at = i / slots;
            const d = Math.abs(at - sweep);
            alpha += 0.55 * Math.max(0, 1 - d * 6);
          }
        } else if (live) {
          alpha = 1;
        }
        ctx.globalAlpha = alpha;
        ctx.beginPath();
        ctx.roundRect?.(x, mid - h / 2, BAR, h, BAR / 2);
        if (!ctx.roundRect) ctx.rect(x, mid - h / 2, BAR, h);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
      ctx.shadowBlur = 0;
      const animate = analyser || (processing && !reduced);
      raf = animate ? requestAnimationFrame(draw) : 0;
    };
    const size = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      width = rect.width;
      height = rect.height;
      rtl = style.direction === "rtl";
      readColors(performance.now());
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // Setting the canvas size clears it. A frozen take has no loop to paint it again.
      if (raf === 0) draw(performance.now());
    };
    size();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(size);
    observer?.observe(canvas);

    return () => {
      cancelAnimationFrame(raf);
      observer?.disconnect();
      void audio?.close().catch(() => undefined);
    };
  }, [stream, processing]);

  return <canvas ref={canvasRef} aria-hidden className={cn("block h-6 min-w-0", className)} />;
}
