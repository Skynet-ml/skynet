import { useSyncExternalStore } from "react";
import type { OptimizationType } from "@/shared/types/api";

/*
 * Remembers each run's kind, and the shape of its page once it has rendered,
 * so the next loading skeleton for that run (a revisit, a reload in the same
 * tab) takes the shape of the page it is about to become instead of jumping
 * on load. A cold first visit has no hint and gets the generic skeleton.
 */

/** How one pipeline stage renders: whether it has a detail line, and what sits under it. */
export interface StageShape {
  detail: boolean;
  /** "status" = a RUNNING/FAILED/SKIPPED line, "time" = a timestamp, "chip" = elapsed chip + timestamp. */
  foot: "none" | "status" | "time" | "chip";
}

/** One block of the Overview tab as it last rendered: its height and the gap above it. */
export interface OverviewBlock {
  kind: "text" | "pipeline" | "block";
  gap: number;
  h: number;
}

export interface RunShape {
  kind?: OptimizationType;
  /** Header text, so the skeleton's lines wrap exactly like the loaded header's. */
  name?: string | null;
  description?: string | null;
  /** The header's cost chip, and its storage link (wide screens only); 44px targets on touch. */
  costChip?: boolean;
  storageLink?: boolean;
  /** The access strip above a run someone else owns; null on the caller's own run. */
  access?: { tier: "editor" | "viewer"; owner?: string | null } | null;
  /**
   * How many icon actions the header's action row holds on wide screens and in
   * the phone shell; null when there is no row at all (an empty row still wraps).
   */
  actions?: { wide: number | null; phone: number | null };
  /** Detail tabs on wide screens and in the phone shell. */
  tabs?: number;
  phoneTabs?: number;
  /** The wide-screen tab bar in order; the phone shell shows its PHONE_DETAIL_TABS subset. */
  tabIds?: string[];
  /** A non-Overview tab's content: its offset in the panel and height, measured at viewport width `vw`. */
  panels?: Record<string, { vw: number; gap?: number; h: number }>;
  /** The Overview's pipeline tracker, one entry per planned stage. */
  stages?: StageShape[];
  /** The Overview's blocks, measured at viewport width `vw`; they wrap differently at any other. */
  overview?: { vw: number; blocks: OverviewBlock[] };
}

// Subpixel heights add up over a long page, so keep two decimals.
const px = (n: number) => Math.round(n * 100) / 100;

/** Measure the Overview tab's blocks (the children of `root`) for its next skeleton. */
export function measureOverview(root: HTMLElement): RunShape["overview"] | undefined {
  let bottom = root.getBoundingClientRect().top;
  const blocks: OverviewBlock[] = [];
  for (const el of Array.from(root.children)) {
    const r = el.getBoundingClientRect();
    if (r.height === 0) continue;
    const kind = el.querySelector("[data-stage-tracker]")
      ? "pipeline"
      : el.firstElementChild?.tagName === "P" || el.tagName === "P"
        ? "text"
        : "block";
    blocks.push({ kind, gap: px(r.top - bottom), h: px(r.height) });
    bottom = r.bottom;
  }
  // A hidden Overview (another tab is open) measures nothing worth keeping.
  return blocks.length ? { vw: window.innerWidth, blocks } : undefined;
}

const STORAGE_PREFIX = "app-run-kind:";
const shapes = new Map<string, RunShape>();

function parse(stored: string): RunShape {
  // Hints written before shapes were stored hold the bare kind.
  if (!stored.startsWith("{")) return { kind: stored as OptimizationType };
  try {
    return JSON.parse(stored) as RunShape;
  } catch {
    return {};
  }
}

function read(key: string): RunShape | undefined {
  const known = shapes.get(key);
  if (known) return known;
  try {
    const stored = window.sessionStorage.getItem(STORAGE_PREFIX + key);
    if (stored) {
      const shape = parse(stored);
      shapes.set(key, shape);
      return shape;
    }
  } catch {
    /* storage unavailable, or rendering on the server */
  }
  return undefined;
}

/** Merge what a render learned about a run's page into its stored shape. */
export function rememberRunShape(key: string, patch: RunShape): void {
  const current = read(key) ?? {};
  const next = { ...current, ...patch };
  if (JSON.stringify(next) === JSON.stringify(current)) return;
  shapes.set(key, next);
  try {
    window.sessionStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(next));
  } catch {
    /* storage unavailable (private mode, quota) — the hint is in-memory only */
  }
}

export function rememberRunKind(id: string, kind: OptimizationType): void {
  rememberRunShape(id, { kind });
}

/**
 * Record what the detail gate's probe learned before the page renders: the
 * kind, and the run's planned stages unless a render already recorded their
 * exact shape. This makes a cold first visit's second skeleton (the one the
 * detail view shows while it fetches) carry the right stage count.
 */
export function rememberProbedRun(
  id: string,
  kind: OptimizationType,
  stages: readonly StageShape[],
): void {
  rememberRunShape(id, read(id)?.stages?.length ? { kind } : { kind, stages: [...stages] });
}

export function runShapeHint(key: string | undefined): RunShape | undefined {
  return key ? read(key) : undefined;
}

// The skeleton reads hints once per mount; nothing needs to re-render when a
// loaded page records a newer shape, since the skeleton is gone by then.
const subscribeNever = () => () => {};

/** The remembered page shape of a run's view; undefined on the server so hydration matches. */
export function useRunShapeHint(key: string | undefined): RunShape | undefined {
  return useSyncExternalStore(
    subscribeNever,
    () => runShapeHint(key),
    () => undefined,
  );
}

/** Tests only: forget every in-memory hint. */
export function resetRunShapeHintsForTests(): void {
  shapes.clear();
}
