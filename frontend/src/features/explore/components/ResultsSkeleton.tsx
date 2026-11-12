"use client";

import * as React from "react";
import { Skeleton } from "@/shared/ui/skeleton";
import { Pagination } from "./Pagination";

// Segmented sm segments are 44px tall below lg and under any coarse pointer
// (the global touch-target rule), 24px otherwise, inside a 2px track inset.
const SEGMENTED_SM_HEIGHT = "h-[48px] lg:h-7 any-pointer-coarse:h-[48px]";

/** What one page of results last rendered, for the next skeleton. */
export interface ExploreResultsLayout {
  /** Per row: [title lines, summary lines] (0 summary lines = no summary). */
  rows: Array<[number, number]>;
  page: number;
  size: number;
  total: number;
}

export function exploreLayoutKey(corpus: string): string {
  return `explore-results:${corpus}`;
}

// A cold first visit: four rows with a one-line title and a two-line summary.
const DEFAULT_ROWS: Array<[number, number]> = [
  [1, 2],
  [1, 2],
  [1, 2],
  [1, 2],
];

interface ResultsSkeletonProps {
  /** The rows to draw; defaults to four typical rows. */
  rows?: Array<[number, number]>;
}

/**
 * Placeholder rows shown while a fresh query is in flight and there are no
 * prior results to keep on screen. Each row is built on ResultRow's own text
 * classes, so its title and summary lines take the real line heights; a
 * revisit draws as many rows, with as many lines, as the page last showed.
 */
function ResultsSkeleton({ rows = DEFAULT_ROWS }: ResultsSkeletonProps) {
  return (
    <ul aria-hidden="true" className="divide-y divide-border/55">
      {rows.map(([titleLines, summaryLines], i) => (
        <li key={i} className="flex flex-col gap-2 rounded-lg px-3 py-4">
          {/* The real tag: the global h3 size wins over the text-* classes. */}
          <h3 className="text-[15.5px] font-medium leading-snug tracking-tight">
            <Skeleton count={Math.max(1, titleLines)} width={`${50 + ((i * 13) % 30)}%`} />
          </h3>
          {summaryLines > 0 && (
            <div className="max-w-[72ch] text-[13.5px] leading-relaxed">
              <Skeleton count={summaryLines} />
            </div>
          )}
          <div className="flex text-[11.5px]">
            <Skeleton width={72} containerClassName="ms-auto" />
          </div>
        </li>
      ))}
    </ul>
  );
}

/** Stand-in for ResultsToolbar: the result count and the sort control. */
function ResultsToolbarSkeleton() {
  return (
    <div
      aria-hidden="true"
      className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 px-1 pb-2"
    >
      <Skeleton height={10} width={64} containerClassName="flex leading-none" />
      <div className="inline-flex items-center gap-1.5">
        <Skeleton width={12} height={12} borderRadius={3} containerClassName="flex leading-none" />
        <Skeleton
          width={176}
          height="100%"
          borderRadius={8}
          containerClassName={`block leading-none ${SEGMENTED_SM_HEIGHT}`}
        />
      </div>
    </div>
  );
}

/** Stand-in for the Explore corpus toggle (full width below sm, as the real one). */
export function CorpusToggleSkeleton() {
  return (
    <Skeleton
      height="100%"
      borderRadius={8}
      containerClassName={`block w-full leading-none sm:w-64 ${SEGMENTED_SM_HEIGHT}`}
    />
  );
}

/**
 * The results pane while a query loads: the toolbar, the rows, and — when the
 * last page had more than one page of results — the pager, kept invisible so
 * it holds its exact height. A single page gets no pager wrapper at all, since
 * an empty one still adds the column gap. A remembered empty page draws nothing.
 */
export function ResultsPaneSkeleton({ layout }: { layout?: ExploreResultsLayout }) {
  if (layout && layout.rows.length === 0) return null;
  return (
    <div className="flex flex-col gap-2" aria-hidden="true">
      <ResultsToolbarSkeleton />
      <div className="border-t border-border/55">
        <ResultsSkeleton rows={layout?.rows} />
      </div>
      {layout && layout.total > layout.size && (
        <div className="invisible">
          <Pagination
            page={layout.page}
            size={layout.size}
            total={layout.total}
            onPageChange={() => {}}
          />
        </div>
      )}
    </div>
  );
}
