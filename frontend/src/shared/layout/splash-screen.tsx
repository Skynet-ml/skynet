"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { AnimatedWordmark } from "@/shared/ui/animated-wordmark";
import { EvolutionMorph } from "@/shared/ui/evolution-morph";

// 1.8 s in all: the morph lands on the human at 1.5 s, the figure and letters
// fade out over 0.2 s, then the backdrop over 0.1 s.
const MORPH_MS = 1500;
const CONTENT_FADE_MS = 200;
const BACKDROP_FADE_MS = 100;
// The letters' variant cross-fade takes 300 ms, so they stop morphing that long
// before the fade and read cleanly when it starts.
const LETTERS_SETTLE_MS = MORPH_MS - 300;
const EASE = "cubic-bezier(0.16, 1, 0.3, 1)";

/**
 * App-load splash: the wordmark letters morphing at the centre of the screen, with
 * the evolution morph playing just above them, held until the figure lands on
 * the human. The content fades before the backdrop so the page's own title
 * never shows through next to the splash letters. Reduced motion shows the
 * still human and letters and skips the hold.
 */
export function SplashScreen() {
  const pathname = usePathname();
  // An agent's consent page is a single focused decision; the brand splash only delays it.
  const skip = pathname === "/mcp/authorize";
  const [phase, setPhase] = useState<"active" | "fading" | "revealing" | "done">(
    skip ? "done" : "active",
  );

  useEffect(() => {
    if (skip) return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const fadeDelay = reduceMotion ? 0 : MORPH_MS;
    const revealDelay = reduceMotion ? 0 : MORPH_MS + CONTENT_FADE_MS;
    const doneDelay = reduceMotion ? 0 : MORPH_MS + CONTENT_FADE_MS + BACKDROP_FADE_MS;
    const timers = [
      setTimeout(() => setPhase("fading"), fadeDelay),
      setTimeout(() => setPhase("revealing"), revealDelay),
      setTimeout(() => setPhase("done"), doneDelay),
    ];

    return () => timers.forEach(clearTimeout);
  }, [skip]);

  if (phase === "done") return null;

  return (
    <div
      className="fixed inset-0 z-[99999] flex items-center justify-center bg-background"
      aria-hidden="true"
      style={{
        // Hidden figure lines are masked with this colour, so it must match the backdrop.
        ["--evolution-ground" as string]: "var(--background)",
        ...(phase === "revealing"
          ? {
              opacity: 0,
              transition: `opacity ${BACKDROP_FADE_MS}ms ${EASE}`,
              pointerEvents: "none" as const,
            }
          : {}),
      }}
    >
      {/* The letters own the centre; the figure hangs above them so its changing
          silhouette never shifts the letters off centre. */}
      <div
        className="relative flex"
        style={
          phase === "active"
            ? undefined
            : { opacity: 0, transition: `opacity ${CONTENT_FADE_MS}ms ${EASE}` }
        }
      >
        <EvolutionMorph
          durationMs={MORPH_MS}
          width={300}
          className="absolute bottom-full left-1/2 mb-2 -translate-x-1/2 motion-reduce:hidden"
        />
        {/* The server can't know the motion preference, so the still human is
            swapped in by CSS; otherwise the fish shows until hydration. */}
        <EvolutionMorph
          still
          width={300}
          className="absolute bottom-full left-1/2 mb-2 hidden -translate-x-1/2 motion-reduce:block"
        />
        {/* 72px-tall letters, narrowed on phones so the six-letter name never
            touches the screen edges. */}
        <div className="w-[min(86vw,372px)]">
          <AnimatedWordmark
            fluid
            autoMorph
            autoMorphDuration={LETTERS_SETTLE_MS}
            morphSpeed={120}
          />
        </div>
      </div>
    </div>
  );
}
