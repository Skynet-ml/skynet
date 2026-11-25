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

// 3 s in all: the journey takes 2.56 s, long enough to see each animal swim or
// walk and the human run and slow to a stand. The human stands for 0.14 s, the
// figure and letters fade out over 0.12 s, then the backdrop over 0.18 s. The
// backdrop fades at an even rate: a steep curve shows an empty screen, then
// the page all at once.
const JOURNEY_MS = 2560;
const REST_MS = 140;
const MORPH_MS = JOURNEY_MS + REST_MS;
const CONTENT_FADE_MS = 120;
const BACKDROP_FADE_MS = 180;
// The letters morph only while the first fish swims, then they stand still: when
// the fish starts to change, the animal is the one thing that moves. The letters
// need about 150 ms more to settle after this time.
const LETTERS_SETTLE_MS = 240;
// With reduced motion the still human stands this long before the fades. With no
// hold the splash is on screen for a few frames only, and that reads as a flash.
const REDUCED_HOLD_MS = 800;

/**
 * App-load splash: the wordmark letters morphing at the centre of the screen, with
 * the evolution journey playing just above them: a fish swims in the sea, climbs
 * the beach as its legs form, and walks on over the land until it runs as the
 * human, who slows down and stands. The splash holds until the human stands. The content fades before the
 * backdrop so the page's own title never shows through next to the splash
 * letters. Reduced motion shows the still human on the land and the letters
 * for a short hold, and keeps the two short fades.
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
    const timer = setTimeout(() => setPhase("landed"), reduceMotion ? REDUCED_HOLD_MS : MORPH_MS);
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
    const next = phase === "fading" ? "revealing" : "done";
    // The two fades also play with reduced motion: a splash that cuts out reads as a flash.
    const timer = setTimeout(
      () => {
        if (next === "done") endSplash("gone");
        setPhase(next);
      },
      phase === "fading" ? CONTENT_FADE_MS : BACKDROP_FADE_MS,
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
              transition: `opacity ${BACKDROP_FADE_MS}ms linear`,
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
            : { opacity: 0, transition: `opacity ${CONTENT_FADE_MS}ms ease-out` }
        }
      >
        <div className="absolute bottom-full left-1/2 mb-3 flex w-max -translate-x-1/2 flex-col items-center">
          <EvolutionMorph
            scene
            durationMs={JOURNEY_MS}
            width={300}
            className="motion-reduce:hidden"
          />
          {/* The server can't know the motion preference, so the still human is
              swapped in by CSS; otherwise the fish shows until hydration. */}
          <EvolutionMorph scene still width={300} className="hidden motion-reduce:block" />
        </div>
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
