"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { useSession } from "next-auth/react";
import {
  SPLASH_HOLD_MAX_MS,
  commitSplashExit,
  endSplash,
  playsEntrance,
  readIntakeDraft,
  readSplashState,
  setupCoversPath,
  splashExit,
  useExperienceOptional,
} from "@/features/experience";
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
 *
 * On a first login the splash is also the setup's entrance: once the figure
 * lands and a setup waits, it hands the screen over with no fade, and the
 * setup goes on from the same frame.
 */
export function SplashScreen() {
  const pathname = usePathname();
  // An agent's consent page is a single focused decision; the brand splash only delays it.
  const skip = pathname === "/mcp/authorize";
  const [phase, setPhase] = useState<"active" | "landed" | "fading" | "revealing" | "done">(
    skip ? "done" : "active",
  );
  const [holdExpired, setHoldExpired] = useState(false);
  const { status } = useSession();
  const experience = useExperienceOptional();
  const exit = splashExit({
    session: status,
    loaded: !!experience?.loaded,
    // Hand off only to a setup that plays the entrance; a resumed one opens elsewhere, so fade.
    setupShows:
      !!experience?.intakeOpen &&
      setupCoversPath(pathname) &&
      playsEntrance({
        languageAnswered: !!readIntakeDraft()?.answered.includes("language"),
        rerunning: experience.rerunning,
        splash: readSplashState(),
      }),
    holdExpired,
  });

  useEffect(() => {
    if (skip) {
      // The Mac app's local sign-in can move /login to the consent page before
      // the morph lands, and this cleanup then drops the landing timer.
      endSplash("gone");
      setPhase("done");
      return;
    }
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timer = setTimeout(() => setPhase("landed"), reduceMotion ? 0 : MORPH_MS);
    return () => clearTimeout(timer);
  }, [skip]);

  useEffect(() => {
    if (phase !== "landed") return;
    if (exit === "hold") {
      const timer = setTimeout(() => setHoldExpired(true), SPLASH_HOLD_MAX_MS);
      return () => clearTimeout(timer);
    }
    commitSplashExit(exit);
    setPhase(exit === "handoff" ? "done" : "fading");
  }, [phase, exit]);

  useEffect(() => {
    if (phase !== "fading" && phase !== "revealing") return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const next = phase === "fading" ? "revealing" : "done";
    const timer = setTimeout(
      () => {
        if (next === "done") endSplash("gone");
        setPhase(next);
      },
      reduceMotion ? 0 : phase === "fading" ? CONTENT_FADE_MS : BACKDROP_FADE_MS,
    );
    return () => clearTimeout(timer);
  }, [phase]);

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
          phase === "active" || phase === "landed"
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
