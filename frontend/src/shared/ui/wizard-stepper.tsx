"use client";

import { useId } from "react";
import { motion } from "framer-motion";
import { Check, type Icon } from "@/shared/ui/icons";
import { cn } from "@/shared/lib/utils";

/**
 * Step indicator at the top of a multi-step wizard. A step with an icon shows
 * it in place of its number, filled once the step is done. Each connector
 * runs between two circle edges and fills once its step is passed.
 */
export function WizardStepper({
  steps,
  step,
  maxReachableStep,
  validateStep,
  onSelect,
  locked = false,
  tutorial,
}: {
  steps: ReadonlyArray<{ id: string; label: string; icon?: Icon }>;
  step: number;
  maxReachableStep: number;
  validateStep: (index: number) => boolean;
  onSelect: (index: number) => void;
  /** A running check holds the wizard where it is. */
  locked?: boolean;
  tutorial?: string;
}) {
  const ringId = useId();

  return (
    <div className="relative" data-tutorial={tutorial}>
      <div className="flex items-center justify-between">
        {steps.map((s, i) => {
          const reachable = i <= maxReachableStep;
          const completed = i < step && validateStep(i);
          const active = i === step;
          const clickable = i <= step || reachable;
          const isLast = i === steps.length - 1;
          const segmentDone = i + 1 <= step;
          const StepIcon = s.icon;
          return (
            <div key={s.id} className="flex flex-col items-center relative z-10 flex-1">
              {!isLast ? (
                <div
                  aria-hidden="true"
                  className="absolute top-[22px] h-px overflow-hidden rounded-full bg-muted/40"
                  style={{
                    insetInlineStart: "calc(50% + 22px)",
                    insetInlineEnd: "calc(-50% + 22px)",
                  }}
                >
                  <motion.div
                    className="h-full rounded-full opacity-40"
                    style={{ background: "var(--gradient-progress)" }}
                    initial={{ width: 0 }}
                    animate={{ width: segmentDone ? "100%" : "0%" }}
                    transition={{ duration: 0.45, ease: [0.2, 0.8, 0.2, 1] }}
                  />
                </div>
              ) : null}
              <button
                type="button"
                onClick={() => onSelect(i)}
                disabled={!clickable || locked}
                aria-label={s.label}
                aria-current={active ? "step" : undefined}
                className={cn(
                  "relative z-10 flex items-center justify-center rounded-full transition-all duration-300",
                  locked ? "cursor-default" : "cursor-pointer",
                  "size-[44px] text-sm font-semibold",
                  active
                    ? "bg-primary-fill text-primary-foreground scale-110"
                    : completed
                      ? "bg-primary-fill/15 text-primary hover:bg-primary-fill/25"
                      : reachable
                        ? "bg-muted text-muted-foreground hover:bg-muted/80 hover:text-foreground"
                        : "bg-muted/50 text-muted-foreground/30 cursor-not-allowed",
                )}
              >
                {StepIcon ? (
                  // The regular weight, filled once done, as the setup draft dialog draws them.
                  <StepIcon
                    className="size-5"
                    weight={completed ? "fill" : "regular"}
                    aria-hidden
                  />
                ) : completed ? (
                  <Check className="size-4" />
                ) : (
                  i + 1
                )}
                {active && (
                  <motion.span
                    layoutId={`${ringId}-step-ring`}
                    className="absolute inset-0 rounded-full border-2 border-primary-fill"
                    transition={{ type: "spring", stiffness: 400, damping: 30 }}
                  />
                )}
              </button>
              <span
                className={cn(
                  "mt-2 text-[0.6875rem] font-medium transition-colors duration-200 hidden sm:block text-center",
                  active ? "text-foreground" : completed ? "text-primary" : "text-muted-foreground",
                )}
              >
                {s.label}
              </span>
            </div>
          );
        })}
      </div>
      <p
        className="mt-2 text-center text-xs font-medium text-foreground sm:hidden"
        aria-live="polite"
      >
        {steps[step]?.label}
      </p>
    </div>
  );
}
