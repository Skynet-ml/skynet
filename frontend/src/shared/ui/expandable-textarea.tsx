"use client";

import * as React from "react";
import { ArrowsIn, ArrowsOut } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Label } from "@/shared/ui/primitives/label";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

interface ExpandableTextareaProps {
  id: string;
  /** The field's label, repeated as the expanded surface's heading and name. */
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  rows?: number;
  /** Classes shared by both textareas; the expanded one adds its own sizing. */
  className?: string;
  /** `data-tutorial` hook for the compact textarea. */
  tutorial?: string;
  readOnly?: boolean;
  /**
   * Receives the compact textarea and the expand trigger so the caller seats
   * them in its own field markup (the trigger usually lives in the label row).
   */
  children: (parts: { textarea: React.ReactNode; trigger: React.ReactNode }) => React.ReactNode;
}

/**
 * A textarea with an "Expand" affordance: the compact control stays in the
 * form, and expanding opens a larger, scrollable editor for the same value on
 * a surface that covers the closest positioned ancestor (the form body), so
 * long text can be read and edited without leaving the step. The caret and
 * selection carry over in both directions, Escape collapses, and the trigger
 * follows the disclosure pattern (aria-expanded / aria-controls).
 */
export function ExpandableTextarea({
  id,
  label,
  value,
  onChange,
  placeholder,
  rows,
  className,
  tutorial,
  readOnly,
  children,
}: ExpandableTextareaProps) {
  const [expanded, setExpanded] = React.useState(false);
  const compactRef = React.useRef<HTMLTextAreaElement>(null);
  const expandedRef = React.useRef<HTMLTextAreaElement>(null);
  const collapseRef = React.useRef<HTMLButtonElement>(null);
  // The caret of whichever textarea the user just left; null until the first
  // toggle so mounting never steals focus.
  const caretRef = React.useRef<[number, number] | null>(null);
  const surfaceId = `${id}-expanded`;
  const expandedInputId = `${surfaceId}-input`;

  const toggle = (next: boolean) => {
    const leaving = next ? compactRef.current : expandedRef.current;
    if (leaving) caretRef.current = [leaving.selectionStart, leaving.selectionEnd];
    setExpanded(next);
  };

  React.useLayoutEffect(() => {
    const caret = caretRef.current;
    if (!caret) return;
    const target = expanded ? expandedRef.current : compactRef.current;
    if (!target) return;
    target.focus({ preventScroll: true });
    target.setSelectionRange(caret[0], caret[1]);
  }, [expanded]);

  const handleSurfaceKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      toggle(false);
      return;
    }
    // The surface covers the form, so Tab cycles between its two controls
    // instead of wandering into fields the user cannot see.
    if (event.key !== "Tab") return;
    const first = collapseRef.current;
    const last = expandedRef.current;
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const textarea = (
    <textarea
      ref={compactRef}
      id={id}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      placeholder={placeholder}
      rows={rows}
      readOnly={readOnly}
      aria-busy={readOnly || undefined}
      dir="auto"
      data-tutorial={tutorial}
      className={className}
    />
  );

  const trigger = (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      onClick={() => toggle(true)}
      aria-expanded={expanded}
      aria-controls={expanded ? surfaceId : undefined}
      aria-label={msg("shared.expandable_textarea.expand")}
    >
      <ArrowsOut className="size-3.5" />
    </Button>
  );

  return (
    <>
      {children({ textarea, trigger })}
      {expanded && (
        <div
          id={surfaceId}
          role="dialog"
          aria-modal="true"
          aria-label={label}
          onKeyDown={handleSurfaceKeyDown}
          className={cn(
            // z-20 so the surface sits above form controls that use z-10 (e.g.
            // the privacy segmented pill), which would otherwise show through it.
            "absolute inset-0 z-20 flex flex-col gap-2 bg-card px-4 py-5 sm:px-6 sm:py-6",
            "motion-safe:animate-in motion-safe:fade-in-0 motion-safe:zoom-in-95 motion-safe:duration-150",
          )}
        >
          <div className="flex shrink-0 items-center justify-between gap-2">
            <Label htmlFor={expandedInputId}>{label}</Label>
            <Button
              ref={collapseRef}
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={() => toggle(false)}
              aria-label={msg("shared.expandable_textarea.collapse")}
            >
              <ArrowsIn className="size-3.5" />
            </Button>
          </div>
          <textarea
            ref={expandedRef}
            id={expandedInputId}
            value={value}
            onChange={(event) => onChange(event.target.value)}
            placeholder={placeholder}
            readOnly={readOnly}
            dir="auto"
            className={cn(className, "min-h-0 flex-1 overflow-y-auto leading-relaxed")}
          />
        </div>
      )}
    </>
  );
}
