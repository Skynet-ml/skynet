"use client";

import { Skeleton } from "@/shared/ui/skeleton";
import * as React from "react";
import { useReducedMotion } from "motion/react";

import { cn } from "@/shared/lib/utils";
import { Check } from "@/shared/ui/icons";
import type { QuestionChoice } from "./types";

interface QuestionChoicesProps {
  options: QuestionChoice[];
  /** Send the picked option's label as the user's answer. */
  onSelect: (label: string, index: number) => void;
  /**
   * Label(s) of the current answer, drawn pressed. For pickers that answer
   * in place (a revisited question, a multi-select confirmed elsewhere);
   * absent, the cards look and behave exactly as before.
   */
  selected?: string | string[];
  disabled?: boolean;
  /** Muted line pointing at the composer for a free-text answer. */
  hint?: string;
  /** Localized accessible name for the option group. */
  ariaLabel?: string;
  className?: string;
  /**
   * Press and hold a card to pick it and move on. A tap (or a short Enter or
   * Space) still only selects through `onSelect`. Absent, cards act on a tap.
   */
  onConfirm?: (label: string, index: number) => void;
}

/** How long a card must be held to confirm it. */
const HOLD_MS = 650;
/** The confirmed card shows its check this long before `onConfirm` moves on. */
const CONFIRM_SHOW_MS = 220;

/**
 * The Claude Code / Codex-style multiple-choice answer picker: 2-4 option
 * cards, each a short label plus a one-line description, selectable by click,
 * digit key, or arrow-then-Enter. Picking one sends its label as the answer;
 * the composer below is always the free-text ("something else") path, so this
 * never renders an explicit "other" card. Rendered in place of a plain quick-
 * reply row beneath an interviewer's question.
 */
export function QuestionChoices({
  options,
  onSelect,
  selected,
  disabled,
  hint,
  ariaLabel,
  className,
  onConfirm,
}: QuestionChoicesProps) {
  const buttonsRef = React.useRef<Array<HTMLButtonElement | null>>([]);
  const [focusedIndex, setFocusedIndex] = React.useState(0);
  const reduceMotion = useReducedMotion() ?? false;
  /** The card under a press that has not ended yet. */
  const [holding, setHolding] = React.useState<number | null>(null);
  const [confirmed, setConfirmed] = React.useState<number | null>(null);
  const holdTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const confirmTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  /** A completed hold also ends in a click, which must not select again. */
  const swallowClick = React.useRef(false);

  React.useEffect(
    () => () => {
      if (holdTimer.current) clearTimeout(holdTimer.current);
      if (confirmTimer.current) clearTimeout(confirmTimer.current);
    },
    [],
  );

  const startHold = (index: number) => {
    if (!onConfirm || disabled || confirmed !== null || holdTimer.current) return;
    swallowClick.current = false;
    setHolding(index);
    holdTimer.current = setTimeout(() => {
      holdTimer.current = null;
      swallowClick.current = true;
      setHolding(null);
      setConfirmed(index);
      navigator.vibrate?.(12);
      const label = options[index]!.label;
      confirmTimer.current = setTimeout(() => onConfirm(label, index), CONFIRM_SHOW_MS);
    }, HOLD_MS);
  };

  /** End a press early; the fill drains back. */
  const cancelHold = () => {
    if (!holdTimer.current) return false;
    clearTimeout(holdTimer.current);
    holdTimer.current = null;
    setHolding(null);
    return true;
  };
  const pressedLabels =
    selected === undefined ? null : new Set(Array.isArray(selected) ? selected : [selected]);

  // A fresh question resets the roving focus to its first option.
  React.useEffect(() => {
    setFocusedIndex(0);
    buttonsRef.current = buttonsRef.current.slice(0, options.length);
  }, [options]);

  const moveFocus = (delta: number) => {
    setFocusedIndex((prev) => {
      const next = (prev + delta + options.length) % options.length;
      buttonsRef.current[next]?.focus();
      return next;
    });
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (disabled) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      moveFocus(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      moveFocus(-1);
    } else if (/^[1-9]$/.test(event.key)) {
      const choice = options[Number(event.key) - 1];
      if (choice) {
        event.preventDefault();
        onSelect(choice.label, Number(event.key) - 1);
      }
    }
  };

  if (options.length === 0) return null;

  return (
    <div
      role="group"
      aria-label={ariaLabel}
      onKeyDown={handleKeyDown}
      className={cn(
        "@container flex flex-col gap-1.5 border-t border-border/40 px-4 pb-1 pt-3",
        "motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-1",
        className,
      )}
    >
      {/* Two columns once the surface fits a label and its description side
          by side, so four options stop stacking into a column that pushes
          the question itself out of the thread above. */}
      <div className="grid grid-cols-1 gap-1.5 @md:grid-cols-2">
        {options.map((option, index) => {
          const pressed = pressedLabels?.has(option.label);
          const isHeld = holding === index;
          const done = confirmed === index;
          return (
            <button
              key={`${index}-${option.label}`}
              ref={(el) => {
                buttonsRef.current[index] = el;
              }}
              type="button"
              disabled={disabled}
              tabIndex={index === focusedIndex ? 0 : -1}
              onFocus={() => setFocusedIndex(index)}
              aria-pressed={pressedLabels ? pressed || done : undefined}
              onClick={() => {
                if (swallowClick.current) {
                  swallowClick.current = false;
                  return;
                }
                onSelect(option.label, index);
              }}
              {...(onConfirm && {
                onPointerDown: (event: React.PointerEvent) => {
                  if (event.button === 0) startHold(index);
                },
                onPointerUp: cancelHold,
                onPointerLeave: cancelHold,
                onPointerCancel: cancelHold,
                // A held finger would open the touch callout or the context menu.
                onContextMenu: (event: React.MouseEvent) => event.preventDefault(),
                onKeyDown: (event: React.KeyboardEvent) => {
                  if (event.key !== "Enter" && event.key !== " ") return;
                  // The key's own click would select at once; the release decides.
                  event.preventDefault();
                  if (!event.repeat) startHold(index);
                },
                onKeyUp: (event: React.KeyboardEvent) => {
                  if (event.key !== "Enter" && event.key !== " ") return;
                  event.preventDefault();
                  if (cancelHold()) onSelect(option.label, index);
                },
              })}
              style={
                onConfirm && !reduceMotion
                  ? {
                      transform: done ? undefined : isHeld ? "scale(0.985)" : undefined,
                      transition: `transform ${isHeld ? HOLD_MS : 160}ms cubic-bezier(0.16, 1, 0.3, 1), background-color 100ms, border-color 100ms`,
                    }
                  : undefined
              }
              className={cn(
                "group flex w-full items-start gap-2.5 rounded-lg border px-3 py-2.5 text-start",
                "transition-colors duration-100 motion-reduce:transition-none",
                onConfirm &&
                  "relative isolate overflow-hidden select-none [-webkit-touch-callout:none] [touch-action:manipulation]",
                done && !reduceMotion && "motion-safe:animate-[choice-confirm_220ms_cubic-bezier(0.16,1,0.3,1)]",
                pressed || done ? "on-primary" : "border-border bg-background",
                "cursor-pointer hover:border-primary-fill/50 hover:bg-primary-fill/5",
                "focus-visible:border-primary-fill/60 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
                "disabled:pointer-events-none disabled:opacity-50",
              )}
            >
              {onConfirm && (
                // The hold fills the card from the side the text starts on, at a
                // steady pace so the user can read how long is left; a release
                // drains it fast. Reduced motion fades the fill in place instead.
                <span
                  aria-hidden
                  className={cn(
                    "pointer-events-none absolute inset-0 -z-10 origin-left rtl:origin-right",
                    pressed ? "bg-primary-foreground/20" : "bg-primary-fill/25",
                  )}
                  style={
                    reduceMotion
                      ? {
                          opacity: isHeld ? 1 : 0,
                          transition: `opacity ${isHeld ? HOLD_MS : 120}ms linear`,
                        }
                      : {
                          transform: `scaleX(${isHeld ? 1 : 0})`,
                          transition: isHeld
                            ? `transform ${HOLD_MS}ms linear`
                            : "transform 180ms cubic-bezier(0.16, 1, 0.3, 1)",
                        }
                  }
                />
              )}
              <span
                aria-hidden
                className={cn(
                  "mt-px flex size-5 shrink-0 items-center justify-center rounded-md text-[11px] font-medium tabular-nums",
                  pressed || done
                    ? "border on-primary"
                    : "border border-border/70 bg-muted text-muted-foreground",
                  "transition-colors duration-100 motion-reduce:transition-none",
                  "group-hover:border-primary-fill/40 group-hover:bg-primary-fill/10 group-hover:text-primary",
                )}
              >
                {done ? <Check className="size-3" /> : index + 1}
              </span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="text-sm font-medium leading-snug text-foreground" dir="auto">
                  {option.label}
                </span>
                {option.description && (
                  <span className="text-xs leading-snug text-muted-foreground" dir="auto">
                    {option.description}
                  </span>
                )}
              </span>
            </button>
          );
        })}
      </div>
      {hint && <p className="px-0.5 pt-0.5 text-xs text-muted-foreground/70">{hint}</p>}
    </div>
  );
}

/**
 * Placeholder rendered while the interviewer is still composing its answer
 * options: two pulsing option-card shapes in `QuestionChoices`'s exact
 * geometry, so the real choices land without a layout shift.
 */
export function QuestionChoicesSkeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden
      className={cn(
        "@container flex flex-col gap-1.5 border-t border-border/40 px-4 pb-1 pt-3",
        "motion-safe:animate-in motion-safe:fade-in-0",
        className,
      )}
    >
      <div className="grid grid-cols-1 gap-1.5 @md:grid-cols-2">
        {[0, 1].map((index) => (
          <div
            key={index}
            className="flex w-full items-start gap-2.5 rounded-lg border border-border/60 bg-background px-3 py-2.5"
          >
            <Skeleton width={20} height={20} containerClassName="mt-px shrink-0 leading-none" />
            <span className="flex min-w-0 flex-1 flex-col gap-1.5 py-0.5">
              <Skeleton height={14} containerClassName="w-2/5 leading-none" />
              <Skeleton height={12} containerClassName="w-3/4 leading-none" />
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
