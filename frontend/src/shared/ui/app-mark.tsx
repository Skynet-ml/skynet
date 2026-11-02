import type { SVGProps } from "react";

/**
 * The app's brand mark: a black and ivory Hoatzin (Opisthocomus hoazin) head with small blue
 * and terracotta eye accents, on a transparent background. The dark artwork is the ivory
 * version so the mark stays visible on a dark surface; the `.dark` class on <html> picks it.
 *
 * Two sizes, picked by rendered size:
 * - "full" (about 64px and up): the 512px artwork.
 * - "simple" (icon sizes): the 256px artwork.
 */

export type HoatzinDetail = "full" | "simple";

// Served from public/brand/, which the auth proxy lets through so signed-out pages show it.
const ARTWORK = {
  full: { light: "/brand/hoatzin-light-512.webp", dark: "/brand/hoatzin-dark-512.webp" },
  simple: { light: "/brand/hoatzin-light-256.webp", dark: "/brand/hoatzin-dark-256.webp" },
} as const;

type Box = { x?: number; y?: number; width?: number | string; height?: number | string };

/**
 * The bird in light and dark artwork, for embedding in another SVG (the wordmark
 * lockup). Fills the given box; the caller picks the detail level for its size.
 */
export function HoatzinShapes({ detail = "full", ...box }: { detail?: HoatzinDetail } & Box) {
  const { light, dark } = ARTWORK[detail];
  return (
    <>
      <image href={light} className="dark:hidden" preserveAspectRatio="xMidYMax meet" {...box} />
      <image href={dark} className="hidden dark:inline" preserveAspectRatio="xMidYMax meet" {...box} />
    </>
  );
}

/** The standalone mark. Defaults to the smaller artwork its icon-sized call sites need. */
export function AppMark({ detail = "simple", ...props }: SVGProps<SVGSVGElement> & { detail?: HoatzinDetail }) {
  return (
    <svg viewBox="0 0 1 1" xmlns="http://www.w3.org/2000/svg" {...props}>
      <HoatzinShapes detail={detail} width={1} height={1} />
    </svg>
  );
}
