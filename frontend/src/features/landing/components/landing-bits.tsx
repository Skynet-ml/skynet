"use client";

/**
 * Small pieces that several landing sections share: the dot, the scroll
 * hooks, the drawn ribbon, the letter roll and the row of cross marks.
 */

import * as React from "react";
import { cn } from "@/shared/lib/utils";

/** Where every "Start a run" goes: sign in first, then the run setup. */
export const RUN_TARGET = `/login?callbackUrl=${encodeURIComponent("/submit")}`;

/** The docs page that explains a run. */
export const HOW_A_RUN_WORKS = "/docs/concepts/optimization";

/** The display face for big titles; Tailwind has no utility for it. */
export const DISPLAY = "[font-family:var(--font-display)] [font-optical-sizing:auto]";

/** The always-dark ink color under the hero, reel and finale. */
export const INK_BG = "bg-[#1c1612] [.dark_&]:bg-[#161210]";

/** A 5px dot in the current text color. */
export function Dot({ className }: { className?: string }) {
  return <span aria-hidden="true" className={cn("size-[5px] flex-none rounded-full bg-current", className)} />;
}

const clamp = (v: number, a: number, b: number) => Math.min(b, Math.max(a, v));

/**
 * How far an element has moved through the viewport: 0 when its top enters
 * at the bottom, 1 when its bottom leaves at the top.
 */
export function viewProgress(el: Element): number {
  const r = el.getBoundingClientRect();
  return clamp((window.innerHeight - r.top) / (window.innerHeight + r.height), 0, 1);
}

export { clamp };

const REDUCE_QUERY = "(prefers-reduced-motion: reduce)";

/** Follow prefers-reduced-motion; false on the server. */
export function useReducedMotion(): boolean {
  return React.useSyncExternalStore(
    (onChange) => {
      const mql = window.matchMedia(REDUCE_QUERY);
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    },
    () => window.matchMedia(REDUCE_QUERY).matches,
    () => false,
  );
}

/**
 * Call `onFrame` once now and at most once per animation frame on scroll and
 * resize. Callers write styles straight to the DOM, so scrolling never
 * re-renders React.
 */
export function useScrollFrame(onFrame: () => void): void {
  const ref = React.useRef(onFrame);
  React.useLayoutEffect(() => {
    ref.current = onFrame;
  });
  React.useEffect(() => {
    let raf = 0;
    const run = () => {
      raf = 0;
      ref.current();
    };
    const request = () => {
      if (!raf) raf = requestAnimationFrame(run);
    };
    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", request);
    run();
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", request);
      window.removeEventListener("resize", request);
    };
  }, []);
}

/**
 * A thick shaded ribbon that draws itself as its section scrolls by. It is
 * mirrored in RTL so it still enters from the reading start.
 */
export function Ribbon({ d, viewBox, className }: { d: string; viewBox: string; className?: string }) {
  const svgRef = React.useRef<SVGSVGElement>(null);
  const reduce = useReducedMotion();
  const len = React.useRef(0);
  useScrollFrame(() => {
    const svg = svgRef.current;
    if (!svg?.parentElement) return;
    const paths = svg.querySelectorAll("path");
    if (!len.current) len.current = (paths[0]?.getTotalLength() ?? 0) * 1.02;
    const p = reduce ? 1 : clamp(viewProgress(svg.parentElement) * 2 - 0.25, 0, 1);
    paths.forEach((el) => {
      el.style.strokeDasharray = `${len.current}`;
      el.style.strokeDashoffset = `${len.current * (1 - p)}`;
    });
  });
  return (
    <svg
      ref={svgRef}
      viewBox={viewBox}
      preserveAspectRatio="none"
      aria-hidden="true"
      className={cn("pointer-events-none absolute z-[2] overflow-visible rtl:-scale-x-100", className)}
    >
      <path d={d} fill="none" strokeLinecap="round" strokeWidth={26} className="translate-y-[5px] stroke-[#6e2f10] opacity-35" />
      <path d={d} fill="none" strokeLinecap="round" strokeWidth={26} className="stroke-primary-fill" />
      <path d={d} fill="none" strokeLinecap="round" strokeWidth={7} className="-translate-x-[2px] -translate-y-[6px] stroke-[#e0935a] opacity-70" />
    </svg>
  );
}

// Letters roll only in scripts that do not join; splitting Arabic or an
// Indic script into boxes would break how the word is drawn.
const ROLLABLE = /^[\p{Script=Latin}\p{Script=Hebrew}\p{Script=Cyrillic}\p{Script=Greek}\p{N}\p{P}\p{S}\s]+$/u;
const HEBREW = /\p{Script=Hebrew}/u;

/**
 * Text whose letters roll up one by one when the nearest `group` is hovered.
 * Screen readers get the plain text once.
 */
export function RollText({ text, className }: { text: string; className?: string }) {
  if (!ROLLABLE.test(text) || typeof Intl.Segmenter !== "function") return <span className={className}>{text}</span>;
  const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
  let i = 0;
  return (
    <span className={className}>
      <span className="sr-only">{text}</span>
      <span aria-hidden="true">
        {text.split(" ").map((word, w) => (
          <React.Fragment key={w}>
            {w > 0 && " "}
            {/* Each word keeps its own direction, so a Latin name stays in order inside a Hebrew line. */}
            <span dir={HEBREW.test(word) ? "rtl" : "ltr"} className="inline-block whitespace-nowrap">
              {Array.from(segmenter.segment(word), ({ segment }) => {
                const n = i++;
                const delay = { transitionDelay: `${n * 16}ms` };
                return (
                  <span key={n} className="inline-block h-[1.15em] overflow-hidden align-top">
                    <i style={delay} className="block not-italic transition-transform duration-[550ms] ease-[cubic-bezier(.16,1,.3,1)] group-hover:-translate-y-full motion-reduce:transition-none">
                      {segment}
                    </i>
                    <i style={delay} className="block not-italic text-primary transition-transform duration-[550ms] ease-[cubic-bezier(.16,1,.3,1)] group-hover:-translate-y-full motion-reduce:transition-none">
                      {segment}
                    </i>
                  </span>
                );
              })}
            </span>
          </React.Fragment>
        ))}
      </span>
    </span>
  );
}

function Cross({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 11 11" className={cn("size-[11px] stroke-foreground", className)} strokeWidth={1.4} aria-hidden="true">
      <path d="M5.5 0v11M0 5.5h11" />
    </svg>
  );
}

/** A row of five cross marks; the inner two hide on phones. */
export function Marks() {
  return (
    <div aria-hidden="true" className="flex items-center justify-between px-0.5 pt-3.5">
      <Cross />
      <Cross className="max-[860px]:hidden" />
      <Cross />
      <Cross className="max-[860px]:hidden" />
      <Cross />
    </div>
  );
}
