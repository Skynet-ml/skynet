"use client";

import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import { cn } from "@/shared/lib/utils";

/**
 * One motion for everything an agent turn opens: the work block, a burst of
 * steps and a step's own result. The panel unrolls from under its header
 * while its content settles down into place.
 */

// An exponential ease-out arrives with confidence; the close uses an ease-in
// so dismissal reads as crisp instead of slow.
const OPEN_EASE = [0.16, 1, 0.3, 1] as const;
const CLOSE_EASE = [0.4, 0, 1, 1] as const;

// Depth sets the duration: the whole block takes longer than one row, which
// keeps the hierarchy of block, burst and row legible.
const DEPTHS = {
  row: { open: 0.2, close: 0.14, rise: 2 },
  block: { open: 0.28, close: 0.18, rise: 4 },
} as const;

/** How deep in the turn the panel sits, which sets how long it takes. */
export type RevealDepth = keyof typeof DEPTHS;

function useMotion(depth: RevealDepth) {
  const reduce = Boolean(useReducedMotion());
  const { open, close, rise } = DEPTHS[depth];
  return {
    reduce,
    rise: reduce ? 0 : rise,
    enter: reduce
      ? { duration: 0 }
      : {
          height: { duration: open, ease: OPEN_EASE },
          // The content follows the height a touch late, so the panel reads
          // as unrolling instead of two things moving at once.
          opacity: { duration: open * 0.8, delay: open * 0.15, ease: "linear" as const },
          y: { duration: open, ease: OPEN_EASE },
        },
    leave: reduce
      ? { duration: 0 }
      : {
          height: { duration: close, ease: CLOSE_EASE },
          opacity: { duration: close * 0.7, ease: "linear" as const },
          y: { duration: 0 },
        },
  };
}

/**
 * A panel that mounts when it opens. Use it where the content holds no state
 * worth keeping, such as a step's result or a burst of step rows.
 */
export function Reveal({
  open,
  depth = "row",
  children,
}: {
  open: boolean;
  depth?: RevealDepth;
  children: React.ReactNode;
}) {
  const { reduce, rise, enter, leave } = useMotion(depth);
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          initial={reduce ? { opacity: 0 } : { height: 0, opacity: 0, y: -rise }}
          animate={{ height: "auto", opacity: 1, y: 0 }}
          // The exit carries its own timing, which runs before the unmount.
          exit={
            reduce
              ? { opacity: 0, transition: leave }
              : { height: 0, opacity: 0, transition: leave }
          }
          transition={enter}
          className="overflow-hidden"
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/**
 * A panel that stays mounted when it folds, so a card waiting on the user
 * keeps its state. Folded, it takes no space and the keyboard skips it.
 */
export function RevealMounted({
  shown,
  depth = "row",
  className,
  children,
}: {
  shown: boolean;
  depth?: RevealDepth;
  className?: string;
  children: React.ReactNode;
}) {
  const { rise, enter, leave } = useMotion(depth);
  return (
    <motion.div
      initial={false}
      animate={shown ? { height: "auto", opacity: 1, y: 0 } : { height: 0, opacity: 0, y: -rise }}
      transition={shown ? enter : leave}
      className={cn("overflow-hidden", className)}
      inert={!shown}
    >
      {children}
    </motion.div>
  );
}
