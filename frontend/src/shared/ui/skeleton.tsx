"use client";

import "react-loading-skeleton/dist/skeleton.css";

import Skeleton, { SkeletonTheme } from "react-loading-skeleton";
import type { ReactNode } from "react";

export { Skeleton };

/**
 * Paints a real component (a badge, a pill, a button) as a bone, so the bone
 * takes that component's exact size at every breakpoint and in every locale.
 * Its text and icons stay laid out but invisible (see `.skeleton-ghost`).
 */
export function Ghost({ children }: { children: ReactNode }) {
  return <span className="skeleton-ghost">{children}</span>;
}

// Tokens, so the bones follow the light/dark theme (see globals.css).
const BASE_COLOR = "var(--skeleton-base)";
const HIGHLIGHT_COLOR = "var(--skeleton-highlight)";
const DURATION_SECONDS = 2.2;
const BORDER_RADIUS = 6;

interface AppSkeletonThemeProps {
  children: ReactNode;
}

export function AppSkeletonTheme({ children }: AppSkeletonThemeProps) {
  return (
    <SkeletonTheme
      baseColor={BASE_COLOR}
      highlightColor={HIGHLIGHT_COLOR}
      duration={DURATION_SECONDS}
      borderRadius={BORDER_RADIUS}
    >
      {children}
    </SkeletonTheme>
  );
}
