"use client";

import * as React from "react";
import { Antigravity, Cline, Codex, Goose, HermesAgent, OpenCode, OpenHands } from "@lobehub/icons";
import type { BlackboxHarness } from "@/shared/types/api";

/**
 * Brand mark for an agent harness, in the same colored-tile style as
 * `ProviderLogo`. Pi has no mark in @lobehub/icons, so it gets a drawn π tile, and
 * Prime Agent carries Prime Intellect's own mark inline for the same reason; a
 * harness without a free, correct mark (custom, aider, eve, deerflow, code_puppy)
 * renders nothing. `dir="ltr"` keeps the mark upright under RTL.
 */
export function HarnessLogo({ harness, size = 20 }: { harness: BlackboxHarness; size?: number }) {
  const mark = renderMark(harness, size);
  if (!mark) return null;
  return (
    <span dir="ltr" className="inline-flex shrink-0">
      {mark}
    </span>
  );
}

// Select triggers and items resize and tint every bare <svg> below them; an
// inline size plus a `text-*` class opts the brand glyph out of both rules.
const GLYPH_CLASS = "text-inherit";

function renderMark(harness: BlackboxHarness, size: number): React.ReactNode {
  const glyphStyle = { width: size, height: size };
  switch (harness) {
    case "pi":
      return <PiMark size={size} />;
    case "codex":
      // @lobehub/icons ships Codex's avatar as white-on-white; the brand tile is a white mark on black.
      return (
        <Codex.Avatar
          size={size}
          background="#000"
          color="#fff"
          iconClassName={GLYPH_CLASS}
          iconStyle={glyphStyle}
        />
      );
    case "opencode":
    case "opencode_v2":
      return <OpenCode.Avatar size={size} iconClassName={GLYPH_CLASS} iconStyle={glyphStyle} />;
    case "openhands":
    case "openhands_sdk":
      return <OpenHands.Avatar size={size} iconClassName={GLYPH_CLASS} iconStyle={glyphStyle} />;
    case "antigravity_sdk":
      return <Antigravity.Avatar size={size} iconClassName={GLYPH_CLASS} iconStyle={glyphStyle} />;
    case "goose":
      return <Goose.Avatar size={size} iconClassName={GLYPH_CLASS} iconStyle={glyphStyle} />;
    case "hermes":
      return <HermesAgent.Avatar size={size} iconClassName={GLYPH_CLASS} iconStyle={glyphStyle} />;
    case "cline":
      return <Cline.Avatar size={size} iconClassName={GLYPH_CLASS} iconStyle={glyphStyle} />;
    case "prime":
      return <PrimeIntellectMark size={size} />;
    default:
      return null;
  }
}

function PiMark({ size }: { size: number }) {
  return (
    <span
      role="img"
      aria-label="Pi"
      className="inline-flex items-center justify-center rounded-full bg-primary-fill text-primary-foreground"
      style={{ width: size, height: size }}
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2.4}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={GLYPH_CLASS}
        style={{ width: size * 0.62, height: size * 0.62 }}
        aria-hidden="true"
      >
        <path d="M4.5 7.5h15" />
        <path d="M9 7.5c0 4.2-.7 7.6-2.4 10.5" />
        <path d="M15.5 7.5v8.6c0 1.3.8 2 1.9 2 .7 0 1.3-.3 1.8-.8" />
      </svg>
    </span>
  );
}

// Prime Intellect's brand mark (primeintellect.ai/icons/primeintellect-logo.svg),
// white on black like its favicon, in the square tile @lobehub/icons avatars use.
const PRIME_INTELLECT_MARK = [
  "M36.71 18.23C36.66 18.23 36.59 18.23 36.51 18.23L36.5 18.22C36.27 18.28 35.97 18.26 35.67 18.24C34.78 18.18 33.82 18.12 34.14 19.95C34.21 20.35 33.72 20.45 33.4 20.46C32.48 20.51 31.55 20.53 30.63 20.52C30.46 20.51 30.28 20.37 30.12 20.23C30.09 20.21 30.06 20.19 30.04 20.17C30.01 20.14 30.12 19.84 30.17 19.84C31.07 19.78 31.4 19.17 31.72 18.57C31.88 18.26 32.05 17.96 32.28 17.72C34.42 15.61 36.6 13.53 39.09 11.83C39.33 11.66 39.63 11.51 39.83 11.8C39.99 12.02 39.84 12.15 39.69 12.28C39.63 12.33 39.56 12.39 39.52 12.46C39.37 12.69 39.1 12.87 38.84 13.05C38.32 13.39 37.8 13.73 38.18 14.53C38.52 15.23 38.22 15.36 37.76 15.54L37.74 15.55C37.55 15.63 37.35 15.71 37.15 15.78C36.72 15.94 36.29 16.1 35.91 16.33C35.53 16.56 35.35 17.01 35.97 17.28C37.34 17.86 40.88 16.93 41.57 15.65C42.22 14.45 43.18 13.56 44.15 12.67C44.69 12.18 45.23 11.68 45.72 11.12C46.76 9.93 47.98 8.9 49.21 7.86C49.81 7.35 50.42 6.83 51.01 6.3C51.45 5.9 51.63 5.37 51.29 4.79C50.94 4.21 50.36 4.13 49.81 4.26C46.19 5.11 42.7 6.26 39.6 8.4C34.65 11.82 29.69 15.23 24.68 18.58C22.98 19.73 21.95 19.17 21.38 17.19C20.11 12.8 18.26 8.97 12.94 8.37C11 8.15 9.57 9.37 9.85 11.3C9.99 12.28 9.87 13.21 9.39 14.12C9.29 14.33 9.18 14.53 9.08 14.73C8.35 16.14 7.62 17.56 7.17 19.05C7.13 19.18 7.09 19.31 7.04 19.44C6.64 20.67 6.12 22.22 8.58 22.31C8.68 22.31 8.87 22.57 8.85 22.66C8.79 22.88 8.69 23.15 8.51 23.28C6.16 25.01 4.22 27.12 3.39 29.96C2.97 31.4 3.24 32.89 4.47 34C5.35 34.8 6.47 35.26 7.45 34.59C8.35 33.98 9.33 33.57 10.32 33.16C11.18 32.8 12.04 32.44 12.84 31.95C13.04 31.83 13.27 31.72 13.51 31.61C14.16 31.3 14.84 30.99 14.72 30.41C14.54 29.52 13.59 28.69 12.8 28.04C12.02 27.39 10.04 23.06 10.27 22.02C10.6 20.56 11.35 19.29 12.1 18.03C12.73 16.95 13.37 15.87 13.75 14.67C13.91 14.17 14.51 13.9 15.1 14.09C15.49 14.23 15.42 14.59 15.36 14.93C15.36 14.94 15.36 14.95 15.36 14.96C15.12 16.26 14.89 17.56 14.66 18.86C14.54 19.51 14.43 20.16 14.31 20.81C14.24 21.22 14.36 21.55 14.8 21.64C15.63 21.8 15.61 22.18 15.23 22.82C14.84 23.5 14.78 24.33 15.23 24.96C15.67 25.59 16.42 25.73 17.18 25.35C17.65 25.11 18.02 25.32 18.01 25.8C17.98 28.34 19.05 27.77 20.34 26.66C20.46 26.56 20.63 26.5 20.78 26.44C20.81 26.43 20.84 26.43 20.87 26.42C20.99 26.37 21.11 26.33 21.24 26.28C23.68 25.42 26.12 24.55 27.5 22.05C27.6 21.86 27.95 21.76 28.22 21.69C28.24 21.69 28.25 21.68 28.27 21.68C29.85 21.25 31.45 21.24 33.06 21.24C34.27 21.23 35.48 21.23 36.67 21.04C37.85 20.86 39.13 20.18 39.14 19.04C39.15 18.12 38.45 18.18 37.75 18.24C37.44 18.26 37.14 18.29 36.89 18.23C36.84 18.22 36.79 18.22 36.71 18.23Z",
  "M18.18 31.05C17.89 32.99 18.63 34.66 21.67 34.63H21.67C24.26 34.53 27.19 32.96 30.06 30.95C31.95 29.63 33.58 28.23 34.65 26.16C35.43 24.65 35.02 23.43 33.85 22.36C33.33 21.89 32.83 21.84 32.25 22.41C30.19 24.42 27.63 25.52 24.99 26.63C24.32 26.91 23.56 27.06 22.79 27.2C20.74 27.58 18.65 27.96 18.18 31.05Z",
];

function PrimeIntellectMark({ size }: { size: number }) {
  return (
    <span
      role="img"
      aria-label="Prime Agent"
      className="inline-flex items-center justify-center bg-black text-white"
      style={{ width: size, height: size, borderRadius: Math.floor(size * 0.1) }}
    >
      <svg
        viewBox="2.48 -5.12 49.63 49.63"
        fill="currentColor"
        className={GLYPH_CLASS}
        style={{ width: size * 0.72, height: size * 0.72 }}
        aria-hidden="true"
      >
        {PRIME_INTELLECT_MARK.map((d) => (
          <path key={d.slice(0, 12)} d={d} />
        ))}
      </svg>
    </span>
  );
}
