"use client";

import type { ComponentType } from "react";

import { OptimizerLogo } from "@/shared/ui/optimizer-logo";
import { Badge } from "@/shared/ui/primitives/badge";
import { msg } from "@/shared/lib/messages";

import {
  BannerFrame,
  GArrow,
  GBar,
  GBox,
  GChart,
  GDot,
  GFrame,
  GMark,
  GNote,
  GPlay,
  GWire,
} from "../steps/PickerSlide";

/** What a card shows about one optimizer: a black-box engine, a DSPy optimizer or Auto. */
export interface EngineSlideInfo {
  id: string;
  label: string;
  description: string;
  available: boolean;
  unavailable_reason?: string | null;
}

/**
 * The body of one optimizer option card: the optimizer's schematic banner,
 * its identity and availability. The caller owns the radio semantics.
 */
export function EngineCard({
  engine,
  blocked = false,
  blockedNote,
  footerSlot = false,
}: {
  engine: EngineSlideInfo;
  // The setup rules this optimizer out; the card stays readable so the
  // user can see what they are giving up.
  blocked?: boolean;
  blockedNote?: string;
  // Reserves a row at the card's foot for controls the caller overlays there,
  // such as source links that cannot live inside the card's button.
  footerSlot?: boolean;
}) {
  const Banner = ENGINE_BANNERS[engine.id] ?? GenericBanner;
  return (
    <>
      {/* The banner frame sizes itself for a full-width slide; a grid card
          only needs a strip of it. */}
      <div className="@container [&>div]:h-24" aria-hidden="true">
        <Banner />
      </div>
      <div className="flex flex-1 flex-col gap-1.5 p-3 text-start">
        <div className="flex flex-wrap items-center gap-2">
          <OptimizerLogo id={engine.id} size={28} className="[&_svg]:size-3.5" />
          <span dir="ltr" className="text-sm font-semibold tracking-tight text-foreground">
            {engine.label}
          </span>
          {!engine.available && (
            <Badge variant="secondary" size="sm">
              {msg("submit.blackbox.engines.not_runnable")}
            </Badge>
          )}
        </div>
        <span className="text-xs leading-relaxed text-muted-foreground">{engine.description}</span>
        {blocked ? (
          <span className="text-xs text-[var(--warning)]" dir="auto">
            {blockedNote ?? msg("submit.blackbox.validation.engine_parts")}
          </span>
        ) : (
          !engine.available &&
          engine.unavailable_reason && (
            <span className="text-xs text-[var(--warning)]" dir="auto">
              {engine.unavailable_reason}
            </span>
          )
        )}
        {footerSlot && <span aria-hidden="true" className="mt-auto h-7 shrink-0" />}
      </div>
    </>
  );
}

// Each banner is the engine's search loop in the wizard's schematic grammar:
// plain boxes are versions, the accented one is the proposer or the winner,
// dashed wires carry feedback rather than candidates.

// Auto races a short trial of several engines in parallel lanes, then hands
// the leader's best version on and keeps running only that engine.
function AutoBanner() {
  return (
    <BannerFrame>
      <GBox x={12} y={33} w={28} h={22} />
      <GBar x={18} y={40} w={16} />
      <GBar x={18} y={46} w={10} />
      <GWire d="M40 44 H50 C58 44 58 18 66 18" />
      <GWire d="M40 44 H66" />
      <GWire d="M40 44 H50 C58 44 58 70 66 70" />
      <GWire d="M114 18 H150" dashed />
      <GWire d="M100 70 H150" dashed />
      <rect x={66} y={15} width={44} height={6} rx={3} fill="var(--foreground)" fillOpacity={0.22} />
      <rect x={66} y={41} width={78} height={6} rx={3} fill="var(--primary)" fillOpacity={0.6} />
      <rect x={66} y={67} width={30} height={6} rx={3} fill="var(--foreground)" fillOpacity={0.22} />
      <GDot cx={110} cy={18} r={3} />
      <GDot cx={144} cy={44} r={3.5} accent />
      <GDot cx={96} cy={70} r={3} />
      <GWire d="M156 8 V80" dashed />
      <GWire d="M148 44 H178" />
      <GArrow x={182} y={44} dir="right" />
      <GBox x={182} y={30} w={44} h={28} accent />
      <GBar x={190} y={38} w={28} />
      <GBar x={190} y={44} w={20} />
      <GBar x={190} y={50} w={24} />
    </BannerFrame>
  );
}

// GEPA reads a version's feedback, reflects it into mutated children, and
// keeps a Pareto front: for every example, the version that scores best on it.
function GepaBanner() {
  return (
    <BannerFrame>
      <GBox x={10} y={33} w={30} h={22} />
      <GBar x={16} y={40} w={18} />
      <GBar x={16} y={46} w={12} />
      <GWire d="M25 33 V20 H46" dashed />
      <GNote x={46} y={9} w={30} h={20} />
      <GBar x={51} y={15} w={16} />
      <GBar x={51} y={21} w={20} />
      <GWire d="M76 19 H86 C92 19 92 34 96 34" dashed />
      <GWire d="M40 44 H92" />
      <GArrow x={96} y={44} dir="right" />
      <GBox x={96} y={30} w={30} h={28} accent />
      <GBar x={103} y={38} w={16} />
      <GBar x={103} y={44} w={11} />
      <GBar x={103} y={50} w={14} />
      <GWire d="M126 44 H132 C138 44 138 18 144 18" />
      <GWire d="M126 44 H144" />
      <GWire d="M126 44 H132 C138 44 138 70 144 70" />
      <GBox x={144} y={10} w={26} h={16} />
      <GBar x={149} y={15} w={14} accent />
      <GBar x={149} y={20} w={10} />
      <GBox x={144} y={36} w={26} h={16} />
      <GBar x={149} y={41} w={10} />
      <GBar x={149} y={46} w={14} accent />
      <GBox x={144} y={62} w={26} h={16} />
      <GBar x={149} y={67} w={12} accent />
      <GBar x={149} y={72} w={8} />
      <GFrame x={178} y={8} w={52} h={72} />
      {[0, 1, 2].map((row) =>
        [0, 1, 2, 3].map((col) => (
          <GDot
            key={`${row}-${col}`}
            cx={188 + col * 11}
            cy={18 + row * 26}
            r={3}
            accent={[1, 0, 2, 0][col] === row}
          />
        )),
      )}
    </BannerFrame>
  );
}

// Best-of-N draws N independent samples of the same prompt; none sees another,
// and the scorer keeps the single best one.
function BestOfNBanner() {
  const samples = [
    { x: 54, score: 9 },
    { x: 80, score: 13 },
    { x: 106, score: 6 },
    { x: 132, score: 17 },
    { x: 158, score: 11 },
  ];
  const picked = 3;
  return (
    <BannerFrame>
      <GBox x={8} y={33} w={30} h={22} />
      <GBar x={14} y={40} w={18} />
      <GBar x={14} y={46} w={12} />
      <GWire d="M38 44 H44 V10 H168" />
      {samples.map(({ x, score }, i) => (
        <g key={x}>
          <GWire d={`M${x + 10} 10 V18`} />
          <GBox x={x} y={18} w={20} h={32} accent={i === picked} />
          <GBar x={x + 4} y={25} w={12} />
          <GBar x={x + 4} y={31} w={9} />
          <GBar x={x + 4} y={37} w={11} />
          <rect
            x={x + 3}
            y={74 - score}
            width={14}
            height={score}
            rx={1.5}
            fill={i === picked ? "var(--primary)" : "var(--foreground)"}
            fillOpacity={i === picked ? 0.75 : 0.2}
          />
        </g>
      ))}
      <GWire d="M50 76 H182" />
      <GWire d="M142 76 V83 H205 V62" />
      <GArrow x={205} y={58} dir="up" />
      <GBox x={190} y={28} w={30} h={30} accent />
      <GBar x={196} y={36} w={18} />
      <GBar x={196} y={42} w={12} />
      <GMark x={205} y={50} ok />
    </BannerFrame>
  );
}

// AutoResearch is an agent in a loop: hypothesize, edit the code, run it,
// read the results, and go again, writing every step into its notebook.
function AutoResearchBanner() {
  return (
    <BannerFrame>
      <GWire d="M80 22 C104 22 116 30 116 40" />
      <GArrow x={116} y={44} dir="down" />
      <GWire d="M116 50 C116 62 104 68 92 68" />
      <GArrow x={88} y={68} dir="left" />
      <GWire d="M68 68 C46 68 34 60 34 50" />
      <GArrow x={34} y={46} dir="up" />
      <GWire d="M34 38 C34 28 46 22 56 22" />
      <GArrow x={60} y={22} dir="right" />
      <GNote x={60} y={12} w={24} h={20} />
      <GBar x={64} y={18} w={12} />
      <GBar x={64} y={24} w={15} />
      <GBox x={102} y={36} w={30} h={18} />
      <GBar x={107} y={41} w={12} />
      <GBar x={111} y={47} w={14} accent />
      <GBox x={66} y={58} w={24} h={20} />
      <GPlay x={78} y={68} />
      <GBox x={18} y={36} w={30} h={18} />
      <GChart x={23} y={51} heights={[5, 8, 6, 11]} accentLast />
      <GBox x={62} y={37} w={26} h={16} accent />
      <GDot cx={70} cy={45} r={2.5} />
      <GDot cx={80} cy={45} r={2.5} />
      <GWire d="M132 45 H162" dashed />
      <GArrow x={166} y={45} dir="right" />
      <GNote x={166} y={8} w={58} h={72} />
      {[16, 24, 32, 40, 48, 56, 64, 72].map((y, i) => (
        <GBar key={y} x={173} y={y} w={[38, 30, 42, 26, 36, 40, 22, 32][i] ?? 30} accent={i === 7} />
      ))}
    </BannerFrame>
  );
}

// Meta-Harness leaves the model alone and has an agent rewrite the code around
// it: the tools it may call, its retry policy, and the glue between them.
function MetaHarnessBanner() {
  return (
    <BannerFrame>
      <GBox x={8} y={30} w={30} h={28} accent />
      <GDot cx={17} cy={41} r={2.5} />
      <GDot cx={29} cy={41} r={2.5} />
      <GBar x={15} y={49} w={16} />
      <GWire d="M38 44 H56" />
      <GArrow x={60} y={44} dir="right" />
      <GFrame x={62} y={6} w={166} h={76} />
      <GBox x={70} y={14} w={44} h={60} />
      <GBar x={76} y={22} w={28} />
      <GBar x={80} y={29} w={24} accent />
      <GBar x={80} y={36} w={18} />
      <GBar x={76} y={43} w={30} />
      <GBar x={80} y={50} w={22} accent />
      <GBar x={80} y={57} w={16} />
      <GBar x={76} y={64} w={24} />
      <GWire d="M114 44 H138" />
      <GBox x={138} y={32} w={36} h={24} />
      <GBar x={145} y={41} w={22} />
      <GBar x={145} y={47} w={14} />
      <GWire d="M174 38 H184 C190 38 190 22 196 22" />
      <GBox x={196} y={13} w={26} h={18} />
      <path
        d="M205 26 L213 18 M211 17 a3 3 0 1 0 3 3"
        fill="none"
        stroke="var(--foreground)"
        strokeOpacity={0.4}
        strokeWidth={1.25}
        strokeLinecap="round"
      />
      <GWire d="M174 50 H184 C190 50 190 64 196 64" />
      <path
        d="M216 64 a7 7 0 1 1 -2 -5"
        fill="none"
        stroke="var(--primary)"
        strokeOpacity={0.8}
        strokeWidth={1.5}
        strokeLinecap="round"
      />
      <GArrow x={215} y={57} dir="up" />
    </BannerFrame>
  );
}

// AutoSaddler circles one candidate through critique, patch and re-score; the
// score trail on the right climbs with every lap.
function AutoSaddlerBanner() {
  return (
    <BannerFrame>
      <GBox x={8} y={34} w={26} h={20} />
      <GBar x={13} y={41} w={16} />
      <GBar x={13} y={46} w={10} />
      <GWire d="M34 44 H56" />
      <GArrow x={60} y={44} dir="right" />
      <path
        d="M100 9 A35 35 0 1 1 99.9 9"
        fill="none"
        stroke="var(--foreground)"
        strokeOpacity={0.22}
        strokeWidth={1.25}
        strokeDasharray="3 3"
      />
      <GArrow x={133} y={56} dir="down" />
      <GArrow x={67} y={56} dir="up" />
      <GArrow x={100} y={79} dir="left" />
      <GBox x={84} y={33} w={32} h={22} accent />
      <GBar x={90} y={40} w={20} />
      <GBar x={90} y={46} w={14} />
      <GNote x={88} y={0} w={24} h={16} />
      <GBar x={92} y={5} w={14} />
      <GBar x={92} y={10} w={10} />
      <GBox x={124} y={58} w={24} h={16} />
      <GBar x={129} y={63} w={14} accent />
      <GBar x={129} y={68} w={9} />
      <GBox x={52} y={58} w={24} h={16} />
      <GChart x={57} y={71} heights={[4, 7, 10]} accentLast />
      <GWire d="M150 44 H164" dashed />
      <GArrow x={168} y={44} dir="right" />
      <GChart x={174} y={74} heights={[10, 18, 24, 32, 40, 48]} accentLast />
      <GWire d="M172 76 H212" />
    </BannerFrame>
  );
}

// ShinkaEvolve keeps islands of candidates; each generation an LLM mutates
// the survivors, selection drops the weak ones, and islands swap migrants.
function ShinkaEvolveBanner() {
  const gens = [12, 82, 152];
  const islands = [8, 48];
  // Per generation, per island: the members selection drops.
  const dropped: number[][][] = [
    [[1], [2]],
    [[0], [1]],
    [[], []],
  ];
  return (
    <BannerFrame>
      {gens.map((gx, g) =>
        islands.map((iy, i) => (
          <g key={`${g}-${i}`}>
            <GFrame x={gx} y={iy} w={44} h={32} />
            {[0, 1, 2].map((k) => (
              <GDot
                key={k}
                cx={gx + 10 + k * 12}
                cy={iy + 16 + (k === 1 ? -5 : 4)}
                accent={g === 2 && i === 0 && k === 1}
                dim={dropped[g]?.[i]?.includes(k) ?? false}
              />
            ))}
          </g>
        )),
      )}
      {[56, 126].map((x) => (
        <g key={x}>
          <GWire d={`M${x} 24 H${x + 6} C${x + 10} 24 ${x + 10} 36 ${x + 13} 38`} />
          <GWire d={`M${x} 64 H${x + 6} C${x + 10} 64 ${x + 10} 52 ${x + 13} 50`} />
          <GBox x={x + 6} y={36} w={14} h={16} accent />
          <GBar x={x + 9} y={43} w={8} />
          <GWire d={`M${x + 20} 40 C${x + 23} 38 ${x + 22} 24 ${x + 26} 24`} />
          <GWire d={`M${x + 20} 48 C${x + 23} 50 ${x + 22} 64 ${x + 26} 64`} />
        </g>
      ))}
      <GWire d="M174 40 V48" dashed />
      <GWire d="M196 24 H210 C218 24 218 44 224 44" />
      <GArrow x={228} y={44} dir="right" />
    </BannerFrame>
  );
}

function GenericBanner() {
  return (
    <BannerFrame>
      <GBox x={60} y={33} w={34} h={22} />
      <GBar x={68} y={40} w={18} />
      <GBar x={68} y={46} w={12} />
      <GWire d="M94 44 H140" />
      <GArrow x={144} y={44} dir="right" />
      <GBox x={144} y={31} w={36} h={26} accent />
      <GBar x={152} y={39} w={20} />
      <GBar x={152} y={45} w={14} />
    </BannerFrame>
  );
}

const ENGINE_BANNERS: Record<string, ComponentType> = {
  auto: AutoBanner,
  gepa: GepaBanner,
  best_of_n: BestOfNBanner,
  autoresearch: AutoResearchBanner,
  meta_harness: MetaHarnessBanner,
  autosaddler: AutoSaddlerBanner,
  shinka_evolve: ShinkaEvolveBanner,
};
