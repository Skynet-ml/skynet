"use client";

/**
 * The page a signed-out visitor sees at `/` (SKY-922). The hero asks for one
 * goal; Send keeps it, and the visitor signs in on the way to the run setup,
 * which opens with that goal. The sections below explain a run.
 */

import * as React from "react";
import { cn } from "@/shared/lib/utils";
import { msg } from "@/shared/lib/messages";
import { HeroComposer } from "./HeroComposer";
import { LandingFinale } from "./LandingFinale";
import { LandingHeader } from "./LandingHeader";
import { BigStatement, Reel, SecondStatement, WorkCards } from "./LandingSections";
import { DISPLAY, Marks, useReducedMotion } from "./landing-bits";
import { SceneCanvas } from "./SceneCanvas";

const TITLE_ID = "landing-ask-title";

/** Render the signed-out landing page. */
export function LandingPage() {
  const [goal, setGoal] = React.useState("");
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);
  const reduce = useReducedMotion();

  const pickGoal = (next: string) => {
    setGoal(next);
    window.scrollTo({ top: 0, behavior: reduce ? "auto" : "smooth" });
    textareaRef.current?.focus({ preventScroll: true });
  };

  return (
    <div
      style={{ "--pad": "clamp(16px, 2.6vw, 36px)" } as React.CSSProperties}
      className="overflow-x-clip bg-background text-foreground"
    >
      <LandingHeader />
      <section className="px-[var(--pad)] pt-[116px] max-[860px]:px-3 max-[860px]:pt-[66px]">
        <div className="relative h-[min(80vh,800px)] min-h-[520px] overflow-hidden rounded-[22px] bg-[radial-gradient(120%_90%_at_70%_30%,#2c2621_0%,#1c1612_62%)] dark:bg-[radial-gradient(120%_90%_at_70%_30%,#2c2621_0%,#161210_62%)] max-[860px]:h-[min(82vh,700px)] max-[860px]:min-h-[560px] max-[860px]:rounded-[18px]">
          <SceneCanvas scene="hero" />
          {/* The composer sits outside any dark scope, so it follows the page theme. */}
          <div className="absolute bottom-7 left-1/2 w-[min(680px,calc(100%-32px))] -translate-x-1/2 max-[860px]:bottom-3 max-[860px]:w-[calc(100%-24px)]">
            <h1
              id={TITLE_ID}
              className={cn(
                "mb-3.5 text-center text-[clamp(26px,3vw,40px)] font-semibold leading-[1.05] tracking-[-.02em] text-[#f3ede6] [text-shadow:0_2px_18px_rgba(0,0,0,.45)] bg-none! [-webkit-text-fill-color:currentColor]!",
                DISPLAY,
              )}
            >
              {msg("landing.ask_title")}
            </h1>
            <HeroComposer goal={goal} onGoalChange={setGoal} textareaRef={textareaRef} titleId={TITLE_ID} />
          </div>
        </div>
        <Marks label={msg("landing.scroll_hint")} />
      </section>
      <BigStatement />
      <Reel />
      <WorkCards onPickGoal={pickGoal} />
      <SecondStatement />
      <LandingFinale />
    </div>
  );
}
