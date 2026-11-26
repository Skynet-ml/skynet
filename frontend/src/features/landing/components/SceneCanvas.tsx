"use client";

/**
 * Mount one landing WebGL scene. three.js loads only here, with a dynamic
 * import, so it never enters the first bundle. The loop pauses off screen,
 * reduced motion draws one still frame, and with no WebGL the canvas goes
 * away and the dark frame behind it stays.
 */

import * as React from "react";
import type { LandingScene } from "../lib/landing-scenes";

/** Render the `hero` field or the finale `tunnel`; `progress` drives the tunnel's flight. */
export function SceneCanvas({
  scene,
  progress,
}: {
  scene: "hero" | "tunnel";
  progress?: React.RefObject<number>;
}) {
  const ref = React.useRef<HTMLCanvasElement>(null);
  const [failed, setFailed] = React.useState(false);

  React.useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let disposed = false;
    let mounted: LandingScene | null = null;
    let raf = 0;
    let io: IntersectionObserver | undefined;
    let ro: ResizeObserver | undefined;

    import("../lib/landing-scenes")
      .then((m) => {
        if (disposed) return;
        mounted =
          scene === "hero"
            ? m.createHeroField(canvas, { reduce })
            : m.createTunnel(canvas, {
                reduce,
                progress: () => progress?.current ?? 0,
                fog: getComputedStyle(canvas.parentElement ?? canvas).backgroundColor,
              });
        const live = mounted;
        if (!live) {
          setFailed(true);
          return;
        }
        live.resize();
        const t0 = performance.now();
        const draw = () => live.render((performance.now() - t0) / 1000);
        if (reduce) {
          draw();
          ro = new ResizeObserver(() => {
            live.resize();
            draw();
          });
          ro.observe(canvas);
          return;
        }
        const loop = () => {
          raf = requestAnimationFrame(loop);
          draw();
        };
        io = new IntersectionObserver(([entry]) => {
          if (!entry) return;
          if (entry.isIntersecting && !raf) loop();
          else if (!entry.isIntersecting && raf) {
            cancelAnimationFrame(raf);
            raf = 0;
          }
        });
        io.observe(canvas);
        ro = new ResizeObserver(() => live.resize());
        ro.observe(canvas);
      })
      .catch(() => {
        if (!disposed) setFailed(true);
      });

    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      io?.disconnect();
      ro?.disconnect();
      mounted?.dispose();
    };
  }, [scene, progress]);

  if (failed) return null;
  return <canvas ref={ref} aria-hidden="true" className="absolute inset-0 block size-full" />;
}
