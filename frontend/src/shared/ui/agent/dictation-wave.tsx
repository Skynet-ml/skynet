"use client";

import * as React from "react";

import { cn } from "@/shared/lib/utils";

import { frameLevel } from "./dictation-level";

// How fast the drawn level follows the mic level, per frame.
const SMOOTHING = 0.25;
// How far the wave moves along the line each frame.
const PHASE_STEP = 0.06;
// One point every 2 CSS pixels keeps the curve smooth at a low cost.
const POINT_STEP = 2;
const ALPHA = 0.7;
// While the transcriber works, the line stays flat and dims to this part of ALPHA.
const PROCESSING_DIM = 0.35;

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
 * A live wave of the dictation take, drawn on a canvas so the mic level never
 * re-renders the composer. While `stream` is live, one thin line in the text
 * color moves with the smoothed mic level. Its envelope is flat at both ends.
 * When `processing` is set, the line stays flat and dims while the transcriber
 * works.
 */
export function DictationWave({
  stream,
  processing = false,
  className,
}: {
  /** The mic stream to listen to; null keeps the line flat. */
  stream: MediaStream | null;
  /** True while the take is with the transcriber. */
  processing?: boolean;
  className?: string;
}) {
  const canvasRef = React.useRef<HTMLCanvasElement | null>(null);

  React.useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

    let analyser: AnalyserNode | null = null;
    let audio: AudioContext | null = null;
    const Ctor = audioContextCtor();
    if (stream && !processing && Ctor) {
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
    let smooth = 0;
    let phase = 0;
    let raf = 0;
    const style = getComputedStyle(canvas);
    const draw = () => {
      let level = 0;
      if (analyser) {
        analyser.getByteTimeDomainData(frame);
        level = frameLevel(frame);
      }
      smooth += (level - smooth) * SMOOTHING;
      if (!reduced) phase += PHASE_STEP;
      const mid = height / 2;
      const amp = processing ? 0 : smooth * (height / 2 - 2);
      ctx.clearRect(0, 0, width, height);
      // The strip fades its text color when the take goes to the transcriber,
      // so the color is read on each frame.
      ctx.strokeStyle = style.color;
      ctx.globalAlpha = ALPHA * (processing ? PROCESSING_DIM : 1);
      ctx.lineWidth = 1.5;
      ctx.lineJoin = "round";
      ctx.beginPath();
      for (let x = 0; x <= width; x += POINT_STEP) {
        const envelope = Math.sin((Math.PI * x) / width);
        const y =
          mid + Math.sin(x / 11 + phase * 3) * Math.sin(x / 37 - phase) * amp * envelope;
        // In RTL the wave moves from the inline start, as it does in LTR.
        const px = rtl ? width - x : x;
        if (x === 0) ctx.moveTo(px, y);
        else ctx.lineTo(px, y);
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
      raf = analyser ? requestAnimationFrame(draw) : 0;
    };
    const size = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      width = rect.width;
      height = rect.height;
      rtl = style.direction === "rtl";
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // Setting the canvas size clears it. A flat line has no loop to paint it again.
      if (raf === 0) draw();
    };
    size();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(size);
    observer?.observe(canvas);
    // The flat line has no loop, so it paints again when the color fade ends.
    const repaint = () => {
      if (raf === 0) draw();
    };
    canvas.addEventListener("transitionend", repaint);

    return () => {
      cancelAnimationFrame(raf);
      canvas.removeEventListener("transitionend", repaint);
      observer?.disconnect();
      void audio?.close().catch(() => undefined);
    };
  }, [stream, processing]);

  return <canvas ref={canvasRef} aria-hidden className={cn("block h-6 min-w-0", className)} />;
}
