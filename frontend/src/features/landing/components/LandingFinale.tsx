"use client";

/**
 * The dark finale: a line whose words scatter as the camera flies through a
 * tunnel of attempts, then the call to action and the footer.
 */

import * as React from "react";
import Link from "next/link";
import { AnimatedWordmark } from "@/shared/ui/animated-wordmark";
import { cn } from "@/shared/lib/utils";
import { msg } from "@/shared/lib/messages";
import { clamp, DISPLAY, Dot, HOW_A_RUN_WORKS, INK_BG, PILL, PILL_GO, RUN_TARGET, useReducedMotion, useScrollFrame } from "./landing-bits";
import { SceneCanvas } from "./SceneCanvas";

const FOOTER_HEAD = "mb-3 text-[11px] font-semibold uppercase tracking-[.1em] text-muted-foreground";
const FOOTER_LINK = "block w-fit py-[3px] hover:text-primary";

/** Render the finale, the call to action and the footer. */
export function LandingFinale() {
  const stageRef = React.useRef<HTMLDivElement>(null);
  const wordRefs = React.useRef<Array<HTMLSpanElement | null>>([]);
  const offsets = React.useRef<number[] | null>(null);
  const progress = React.useRef(0);
  const reduce = useReducedMotion();
  const line = msg("landing.finale_words");
  const words = line.split(" ");

  React.useEffect(() => {
    const reset = () => (offsets.current = null);
    window.addEventListener("resize", reset);
    return () => window.removeEventListener("resize", reset);
  }, []);

  useScrollFrame(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const r = stage.getBoundingClientRect();
    const pf = reduce ? 0 : clamp(-r.top / (r.height - window.innerHeight), 0, 1);
    progress.current = pf;
    const els = wordRefs.current;
    const cx = window.innerWidth / 2;
    if (!offsets.current) {
      // Measure each word's distance from the center without the current transform.
      els.forEach((el) => el && (el.style.transform = ""));
      offsets.current = els.map((el) => {
        if (!el) return 0;
        const b = el.getBoundingClientRect();
        return b.left + b.width / 2 - cx;
      });
    }
    const e = clamp((pf - 0.25) / 0.6, 0, 1) ** 2;
    els.forEach((el, i) => {
      if (!el) return;
      const c = offsets.current?.[i] ?? 0;
      const dx = Math.sign(c || (i % 2 ? 1 : -1)) * (Math.abs(c) / cx + 0.3) * e * 60;
      const dy = ((i % 3) - 1) * e * 18;
      el.style.transform = `translate(${dx}vw, ${dy}vh) scale(${1 + e * 0.6})`;
      el.style.opacity = `${1 - e}`;
      el.style.filter = `blur(${e * 10}px)`;
    });
  });

  return (
    <section
      data-landing-ink
      className={cn("dark relative overflow-hidden rounded-t-[28px] text-foreground max-[860px]:rounded-t-[20px]", INK_BG)}
    >
      <SceneCanvas scene="tunnel" progress={progress} />
      <div ref={stageRef} className="relative h-[180vh] max-[860px]:h-[150vh]">
        <div className="sticky top-0 grid h-screen place-items-center px-[var(--pad)]">
          <h2
            aria-label={line}
            className={cn(
              "max-w-[18ch] text-center text-[clamp(34px,5.2vw,76px)] font-medium uppercase leading-[1.04] tracking-[-.02em]",
              DISPLAY,
            )}
          >
            {words.map((word, i) => (
              <React.Fragment key={i}>
                {i > 0 && " "}
                <span
                  aria-hidden="true"
                  ref={(el) => void (wordRefs.current[i] = el)}
                  className="inline-block will-change-[transform,opacity,filter]"
                >
                  {word}
                </span>
              </React.Fragment>
            ))}
          </h2>
        </div>
      </div>
      <div className="relative px-[var(--pad)] pb-9">
        <div className="grid grid-cols-2 items-end gap-12 max-[860px]:grid-cols-1 max-[860px]:gap-5">
          <h3 className={cn("max-w-[13ch] text-[clamp(36px,4.4vw,64px)] font-medium leading-[1.02] tracking-[-.035em]", DISPLAY)}>
            {msg("landing.cta_title")}
          </h3>
          <div>
            <p className="mb-[22px] max-w-[40ch] text-[17px] text-muted-foreground">{msg("landing.cta_body")}</p>
            <Link href={RUN_TARGET} className={cn(PILL, PILL_GO, "h-12 px-6 text-[13px] max-[860px]:h-12 max-[860px]:px-6")}>
              {msg("landing.start_run")} <Dot />
            </Link>
          </div>
        </div>
        <footer className="mt-[140px] grid grid-cols-[2fr_1fr_1fr_1fr] gap-6 border-t border-border pt-7 text-sm max-[860px]:mt-[90px] max-[860px]:grid-cols-2">
          <div className="max-[860px]:col-span-full">
            <Link href="/" aria-label={msg("legal.chrome.home_aria")} className="inline-flex">
              <AnimatedWordmark size={22} withMark />
            </Link>
          </div>
          <div>
            <h4 className={FOOTER_HEAD}>{msg("landing.footer_product")}</h4>
            <Link href={RUN_TARGET} className={FOOTER_LINK}>
              {msg("landing.start_run")}
            </Link>
            <Link href="/docs" className={FOOTER_LINK}>
              {msg("landing.nav_docs")}
            </Link>
          </div>
          <div>
            <h4 className={FOOTER_HEAD}>{msg("landing.footer_read")}</h4>
            <Link href="/blog" className={FOOTER_LINK}>
              {msg("landing.nav_blog")}
            </Link>
            <Link href={HOW_A_RUN_WORKS} className={FOOTER_LINK}>
              {msg("landing.how_run_works")}
            </Link>
          </div>
          <div>
            <h4 className={FOOTER_HEAD}>{msg("landing.footer_legal")}</h4>
            <Link href="/privacy" className={FOOTER_LINK}>
              {msg("landing.footer_privacy")}
            </Link>
            <Link href="/terms" className={FOOTER_LINK}>
              {msg("landing.footer_terms")}
            </Link>
          </div>
        </footer>
        <p className="mt-[60px] text-[13px] text-muted-foreground">{msg("landing.legal_line")}</p>
      </div>
    </section>
  );
}
