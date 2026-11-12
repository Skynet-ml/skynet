"use client";
import * as React from "react";
import { ExpandToggleButton } from "@/shared/ui/expand-toggle-button";
import { cn } from "@/shared/lib/utils";
import type { ParsedDataset } from "@/shared/lib/parse-dataset";
import { DatasetRowsView } from "./DatasetRowsView";

/**
 * The one dataset preview used across the app: the sortable, filterable rows
 * grid, double-click (or tap on touch) to read a whole row, and a toggle that
 * grows the table. Escape peels one layer at a time: reader, then expansion,
 * then whatever hosts the panel.
 *
 * ``expanded`` may be controlled so a host dialog can grow alongside the
 * table; left uncontrolled the panel keeps its own state.
 */
export function DatasetPreviewPanel({
  rows,
  filename,
  emptyTitle,
  expanded: expandedProp,
  onExpandedChange,
  className,
  expandedClassName,
}: {
  /** ``null`` while loading. */
  rows: Pick<ParsedDataset, "columns" | "rows"> | null;
  filename?: string;
  emptyTitle?: string;
  expanded?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
  /** Height of the collapsed panel. */
  className?: string;
  /** Height of the expanded panel. */
  expandedClassName?: string;
}) {
  const id = React.useId();
  const panelRef = React.useRef<HTMLDivElement>(null);
  const expandButton = React.useRef<HTMLButtonElement>(null);
  const [readerIndex, setReaderIndex] = React.useState<number | null>(null);
  const [expandedState, setExpandedState] = React.useState(false);
  const expanded = expandedProp ?? expandedState;
  const setExpanded = (next: boolean) => {
    if (expandedProp === undefined) setExpandedState(next);
    onExpandedChange?.(next);
  };

  // Dialog hosts (Radix) catch Escape on the document in the capture phase,
  // before any React handler runs. A window capture listener runs first, so
  // Escape can close the reader, then the expansion, before the dialog.
  const layered = readerIndex !== null || expanded;
  React.useEffect(() => {
    if (!layered) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const panel = panelRef.current;
      if (event.key !== "Escape" || !panel?.contains(event.target as Node)) return;
      event.preventDefault();
      event.stopPropagation();
      if (readerIndex !== null) {
        setReaderIndex(null);
        panel.focus({ preventScroll: true });
      } else {
        if (expandedProp === undefined) setExpandedState(false);
        onExpandedChange?.(false);
        expandButton.current?.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown, { capture: true });
    return () => window.removeEventListener("keydown", onKeyDown, { capture: true });
  }, [layered, readerIndex, expandedProp, onExpandedChange]);

  return (
    <div
      id={id}
      ref={panelRef}
      tabIndex={-1}
      className={cn(
        "flex min-h-0 min-w-0 flex-col overflow-hidden rounded-xl border border-border outline-none transition-[height] duration-200 ease-out motion-reduce:transition-none",
        expanded ? (expandedClassName ?? "h-[70dvh] min-h-96") : (className ?? "h-96"),
      )}
    >
      <DatasetRowsView
        rows={rows}
        filename={filename}
        emptyTitle={emptyTitle}
        readerIndex={readerIndex}
        setReaderIndex={setReaderIndex}
        toolbarActions={
          <ExpandToggleButton
            ref={expandButton}
            expanded={expanded}
            controls={id}
            onToggle={() => setExpanded(!expanded)}
          />
        }
      />
    </div>
  );
}

/** The grow/shrink toggle every dataset table puts in its toolbar. */
