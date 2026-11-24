"use client";

/**
 * The landing sections between the hero and the finale: the big statement,
 * the "Watch a run" reel, the goal cards and the second statement.
 */

import * as React from "react";
import Link from "next/link";
import { Play } from "@/shared/ui/icons";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/primitives/button";
import { msg, type MessageKey } from "@/shared/lib/messages";
import {
  clamp,
  DISPLAY,
  HOW_A_RUN_WORKS,
  INK_BG,
  Marks,
  Ribbon,
  RollText,
  useReducedMotion,
  useScrollFrame,
  viewProgress,
} from "./landing-bits";

const EASE = "ease-[cubic-bezier(.16,1,.3,1)]";

/** A seeded generator, so the art is the same on the server and the client. */
function seeded(seed: number) {
  let s = seed;
  return () => (s = (s * 16807) % 2147483647) / 2147483647;
}

/** Render "Name the score" with its ribbon and the short explanation beside it. */
export function BigStatement() {
  return (
    <section className="relative px-[var(--pad)] pb-[60px] pt-[clamp(130px,15vw,210px)]">
      <Ribbon
        viewBox="0 0 800 360"
        d="M-30 30 C 200 10, 330 120, 470 190 S 700 300, 830 340"
        className="start-0 top-10 h-[360px] w-[62%] max-[860px]:top-[70px] max-[860px]:h-60 max-[860px]:w-full"
      />
      <h2
        className={cn(
          "relative z-[1] mx-auto max-w-[12ch] text-balance text-center text-[clamp(52px,8.4vw,96px)] font-medium leading-[.98] tracking-[-.04em]",
          DISPLAY,
        )}
      >
        {msg("landing.big_title")}
      </h2>
      <div className="mt-[90px] grid grid-cols-2 gap-12 max-[860px]:mt-14 max-[860px]:grid-cols-1">
        <div className="col-start-2 max-w-[46ch] max-[860px]:col-start-1">
          <p className="mb-[22px] text-[17px] leading-[1.55]">{msg("landing.big_body")}</p>
          <Button asChild variant="outline">
            <Link href={HOW_A_RUN_WORKS}>{msg("landing.how_run_works")}</Link>
          </Button>
        </div>
      </div>
    </section>
  );
}

const REEL_LINE = "M80 500 H210 V470 H330 V470 H420 V410 H560 V380 H650 V300 H790 V280 H880 V210 H1000 V160 H1120";
const REEL_DOTS = (() => {
  const r = seeded(11);
  return Array.from({ length: 90 }, (_, i) => {
    const top = 500 - (i / 90) * 340;
    return { cx: 90 + i * 11.5, cy: top + r() * 90, r: 2 + r() * 2 };
  });
})();

/** Render the dark reel: a score line that climbs, with "Watch [play] a run" split around it. */
export function Reel() {
  const sectionRef = React.useRef<HTMLElement>(null);
  const lineRef = React.useRef<SVGPathElement>(null);
  const wordsRef = React.useRef<Array<HTMLSpanElement | null>>([]);
  const reduce = useReducedMotion();

  useScrollFrame(() => {
    const section = sectionRef.current;
    const line = lineRef.current;
    if (!section || !line) return;
    const p = viewProgress(section);
    const len = line.getTotalLength();
    line.style.strokeDasharray = `${len}`;
    line.style.strokeDashoffset = `${reduce ? 0 : len * (1 - clamp(p * 2.2 - 0.3, 0, 1))}`;
    // The words part toward the screen edges; in RTL the first word sits on the right.
    const side = getComputedStyle(section).direction === "rtl" ? -1 : 1;
    const pr = reduce || window.innerWidth < 860 ? 0 : clamp((p - 0.35) / 0.4, 0, 1);
    const [first, last] = wordsRef.current;
    if (first) first.style.transform = `translateX(${-pr * 16 * side}vw)`;
    if (last) last.style.transform = `translateX(${pr * 16 * side}vw)`;
  });

  return (
    <section ref={sectionRef} className="px-[var(--pad)] pt-10">
      <div data-landing-ink className={cn("dark relative h-[min(70vh,700px)] min-h-[380px] overflow-hidden rounded-[22px] text-foreground", INK_BG)}>
        <svg viewBox="0 0 1200 600" preserveAspectRatio="xMidYMid slice" aria-hidden="true" className="absolute inset-0 size-full">
          <path d="M80 120H1120M80 240H1120M80 360H1120M80 480H1120" className="stroke-[#3a312a]" strokeWidth={1} />
          <g className="fill-[#6b5d50]">
            {REEL_DOTS.map((d, i) => (
              <circle key={i} cx={d.cx} cy={d.cy} r={d.r} />
            ))}
          </g>
          <path ref={lineRef} d={REEL_LINE} fill="none" className="stroke-[#e0935a]" strokeWidth={2.5} strokeLinejoin="round" />
          <circle cx={1120} cy={160} r={9} className="fill-primary-fill" />
        </svg>
        <div
          className={cn(
            "absolute inset-0 flex items-center justify-center gap-[clamp(14px,2.4vw,36px)] whitespace-nowrap text-[clamp(56px,10vw,150px)] font-medium leading-none tracking-[-.045em] text-[#f3ede6] max-[860px]:flex-col max-[860px]:gap-3.5 max-[860px]:text-[64px]",
            DISPLAY,
          )}
        >
          <span ref={(el) => void (wordsRef.current[0] = el)} className="will-change-transform">
            {msg("landing.reel_watch")}
          </span>
          <Link
            href={HOW_A_RUN_WORKS}
            aria-label={msg("landing.reel_play")}
            className={cn(
              "grid h-[clamp(52px,5.4vw,76px)] w-[clamp(84px,9vw,128px)] flex-none place-items-center rounded-xl bg-[#f3ede6] text-[#1c1612] transition-[transform,background-color] duration-[400ms] hover:scale-[1.06] hover:bg-white active:scale-[0.97] focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
              EASE,
            )}
          >
            <Play weight="fill" className="size-[22px]" />
          </Link>
          <span ref={(el) => void (wordsRef.current[1] = el)} className="will-change-transform">
            {msg("landing.reel_a_run")}
          </span>
        </div>
      </div>
      <Marks />
    </section>
  );
}

const CARD_ART = "aspect-[16/10] overflow-hidden rounded-[18px] border border-border bg-secondary transition-[border-radius] duration-[600ms] group-hover:rounded-[40px]";
const CARD_SVG = "block size-full transition-transform duration-1000 group-hover:scale-[1.05]";
const CARD_TITLE = cn("mt-4 block text-[clamp(24px,2.4vw,32px)] font-medium leading-[1.15] tracking-[-.02em]", DISPLAY);
const CARD_TAG = "mt-1.5 block text-[11px] font-semibold uppercase tracking-[.1em] text-muted-foreground";

const GOAL_CARDS: Array<{ title: MessageKey; tag: MessageKey; goal: MessageKey; art: React.ReactNode }> = [
  {
    title: "landing.card_tests_title",
    tag: "landing.card_tests_tag",
    goal: "landing.card_tests_goal",
    art: (
      <>
        <rect width="320" height="200" className="fill-secondary" />
        <g className="fill-border">
          <rect x="40" y="44" width="240" height="10" rx="5" />
          <rect x="40" y="66" width="200" height="10" rx="5" />
          <rect x="40" y="88" width="170" height="10" rx="5" />
          <rect x="40" y="110" width="120" height="10" rx="5" />
        </g>
        <rect x="40" y="132" width="78" height="10" rx="5" className="fill-primary-fill" />
        <path d="M40 164h240" fill="none" className="stroke-foreground" strokeWidth={1} strokeDasharray="2 4" />
      </>
    ),
  },
  {
    title: "landing.card_eval_title",
    tag: "landing.card_eval_tag",
    goal: "landing.card_eval_goal",
    art: (
      <>
        <rect width="320" height="200" className="fill-[#1c1612] [.dark_&]:fill-[#161210]" />
        <g className="fill-[#6b5d50]">
          {[[50, 150], [72, 136], [90, 158], [112, 120], [130, 140], [152, 104], [170, 126], [196, 92], [214, 110], [240, 78]].map(([cx, cy]) => (
            <circle key={cx} cx={cx} cy={cy} r={3} />
          ))}
        </g>
        <path d="M50 150 L112 120 L152 104 L196 92 L240 78 L270 62" fill="none" className="stroke-primary-fill" strokeWidth={2} />
        <circle cx="270" cy="62" r="7" className="fill-primary-fill" />
      </>
    ),
  },
  {
    title: "landing.card_cost_title",
    tag: "landing.card_cost_tag",
    goal: "landing.card_cost_goal",
    art: (
      <>
        <rect width="320" height="200" className="fill-secondary" />
        <path d="M30 50 C 110 60, 150 140, 290 150 L290 200 L30 200Z" className="fill-primary-fill opacity-[.16]" />
        <path d="M30 50 C 110 60, 150 140, 290 150" fill="none" className="stroke-foreground" strokeWidth={2} />
        <circle cx="290" cy="150" r="6" className="fill-primary-fill" />
      </>
    ),
  },
];

/** Render the goal cards; picking one hands its goal to `onPickGoal`. */
export function WorkCards({ onPickGoal }: { onPickGoal: (goal: string) => void }) {
  return (
    <section className="px-[var(--pad)] pb-10 pt-[150px] max-[860px]:pt-[110px]">
      <div className="mb-9 flex items-end justify-between gap-6 max-[860px]:flex-col max-[860px]:items-start">
        <h2 className={cn("text-[clamp(44px,6.4vw,88px)] font-medium leading-none tracking-[-.04em]", DISPLAY)}>
          {msg("landing.work_title")}
        </h2>
        <p className="max-w-[34ch] text-pretty text-end text-[11px] font-semibold uppercase leading-normal tracking-[.08em] text-muted-foreground max-[860px]:text-start">
          {msg("landing.work_hint")}
        </p>
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-14 max-[860px]:grid-cols-1 max-[860px]:gap-10">
        {GOAL_CARDS.map((card) => (
          <button
            key={card.title}
            type="button"
            onClick={() => onPickGoal(msg(card.goal))}
            className="group block cursor-pointer text-start focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-4 focus-visible:ring-offset-background"
          >
            <span className={cn(CARD_ART, "block", EASE)}>
              <svg viewBox="0 0 320 200" preserveAspectRatio="xMidYMid slice" aria-hidden="true" className={cn(CARD_SVG, EASE)}>
                {card.art}
              </svg>
            </span>
            <RollText text={msg(card.title)} className={CARD_TITLE} />
            <span className={CARD_TAG}>{msg(card.tag)}</span>
          </button>
        ))}
        <Link
          href="/blog"
          className="group block text-start focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-4 focus-visible:ring-offset-background"
        >
          <span className={cn(CARD_ART, "block", EASE)}>
            <svg viewBox="0 0 320 200" preserveAspectRatio="xMidYMid slice" aria-hidden="true" className={cn(CARD_SVG, EASE)}>
              <rect width="320" height="200" fill="#b4521f" />
              {/* Anchored from the left in every locale, so a Hebrew word stays inside the art. */}
              <text x="28" y="120" direction="ltr" fontSize="64" fontWeight="500" fill="#fcfaf7" letterSpacing="-2" style={{ fontFamily: "var(--font-display)" }}>
                {msg("landing.card_essay_art")}
              </text>
              <path d="M28 140h120" stroke="#fcfaf7" strokeWidth={2} />
            </svg>
          </span>
          <RollText text={msg("landing.card_essay_title")} className={CARD_TITLE} />
          <span className={CARD_TAG}>{msg("landing.card_essay_tag")}</span>
        </Link>
      </div>
      <div className="relative flex justify-center pb-10 pt-[90px]">
        <Ribbon
          viewBox="0 0 1400 120"
          d="M300 30 C 520 -10, 700 30, 820 70 S 1150 110, 1440 90"
          className="-start-[var(--pad)] top-[50px] z-0 h-[120px] w-[calc(100%+var(--pad)*2)] max-[860px]:hidden"
        />
        <Button asChild variant="outline" className="relative z-[3]">
          <Link href="/blog">{msg("landing.read_blog")}</Link>
        </Button>
      </div>
    </section>
  );
}

type TreeNode = { x: number; y: number; parent: number; depth: number };

const TREE = (() => {
  const r = seeded(11);
  const nodes: TreeNode[] = [{ x: 240, y: 70, parent: -1, depth: 0 }];
  for (let d = 1; d < 5; d++) {
    const prev = nodes.filter((n) => n.depth === d - 1);
    for (const p of prev) {
      const k = d === 1 ? 3 : 2;
      for (let c = 0; c < k; c++) {
        nodes.push({ x: p.x + (c - (k - 1) / 2) * (150 / d) + (r() - 0.5) * 10, y: 70 + d * 52, parent: nodes.indexOf(p), depth: d });
      }
    }
  }
  const best = nodes.length - 3;
  const chain = new Set<number>();
  for (let i = best; i >= 0; i = nodes[i]!.parent) chain.add(i);
  return { nodes, best, chain };
})();

/** Render "One sentence in" with the device that shows a small search tree. */
export function SecondStatement() {
  const { nodes, best, chain } = TREE;
  return (
    <section className="relative px-[var(--pad)] pb-[170px] pt-[110px]">
      <Ribbon
        viewBox="0 0 500 640"
        d="M520 -10 C 300 60, 120 120, 200 260 S 480 420, 340 560 S 120 620, 60 660"
        className="end-0 top-[120px] h-[640px] w-[52%] max-[860px]:hidden"
      />
      <h2 className={cn("relative z-[1] max-w-[14ch] text-[clamp(46px,6.6vw,92px)] font-medium leading-none tracking-[-.04em]", DISPLAY)}>
        {msg("landing.second_title")}
      </h2>
      <div className="mt-16 grid grid-cols-[1.15fr_1fr] items-center gap-[clamp(32px,6vw,96px)] max-[860px]:mt-10 max-[860px]:grid-cols-1">
        <div className="relative z-[1] aspect-[4/3] rounded-[30px] bg-[#1c1612] p-3.5 shadow-[0_2px_6px_rgba(28,22,18,.12),0_40px_70px_-30px_rgba(28,22,18,.45)] dark:bg-[#0f0c0a] dark:shadow-[0_0_0_1px_var(--border),0_40px_70px_-30px_rgba(0,0,0,.7)]">
          <div className="size-full overflow-hidden rounded-[18px] bg-background">
            <svg viewBox="0 0 480 360" aria-hidden="true" className="block size-full">
              <rect width="480" height="44" className="fill-secondary" />
              <circle cx="26" cy="22" r="6" className="fill-primary-fill" />
              <rect x="42" y="17" width="120" height="10" rx="5" className="fill-border" />
              <rect x="372" y="14" width="84" height="16" rx="8" className="fill-border" />
              <g fill="none">
                {nodes.map((n, i) => {
                  if (n.parent < 0) return null;
                  const p = nodes[n.parent]!;
                  const on = chain.has(i);
                  const midY = (p.y + n.y) / 2;
                  return (
                    <path
                      key={i}
                      d={`M${p.x} ${p.y} C ${p.x} ${midY}, ${n.x} ${midY}, ${n.x} ${n.y}`}
                      className={on ? "stroke-primary-fill" : "stroke-foreground"}
                      opacity={on ? 1 : 0.22}
                      strokeWidth={on ? 2.5 : 1.5}
                    />
                  );
                })}
                {nodes.map((n, i) => (
                  <circle key={i} cx={n.x} cy={n.y} r={i === best ? 7 : 4} className={chain.has(i) ? "fill-primary-fill" : "fill-border"} />
                ))}
              </g>
              <rect x="24" y="296" width="432" height="44" rx="10" className="fill-secondary" />
              <rect x="40" y="314" width="8" height="8" rx="4" className="fill-primary-fill" />
              <rect x="58" y="313" width="160" height="10" rx="5" className="fill-border" />
              <rect x="388" y="308" width="52" height="20" rx="10" className="fill-primary-fill" />
            </svg>
          </div>
        </div>
        <div className="relative z-[1] max-w-[40ch] text-[17px] leading-[1.55]">
          <p>{msg("landing.second_body1")}</p>
          <p className="mt-[18px] text-muted-foreground">{msg("landing.second_body2")}</p>
        </div>
      </div>
    </section>
  );
}
