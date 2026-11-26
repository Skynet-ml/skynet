"use client";

import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";

/**
 * The motions an agent turn opens and closes with, timed as in Codex: a
 * disclosure (a burst of steps, a step's own result) unrolls and fades, and
 * the turn's history also drops into place as it opens.
 */

// Codex's enter curve and relaxed duration open both; the history closes
// faster on its snappy curve, so a fold reads as crisp, not slow.
const OPEN_EASE = [0.19, 1, 0.22, 1] as const;
const CLOSE_EASE = [0.23, 1, 0.32, 1] as const;
const OPEN_S = 0.3;
export const CLOSE_S = 0.15;
const RISE_PX = 8;

const DISCLOSURE = { duration: OPEN_S, ease: OPEN_EASE };
const HISTORY_CLOSE = { duration: CLOSE_S, ease: CLOSE_EASE };
// Reduced motion keeps the history's fade but drops the drop and unroll.
const REDUCED_HISTORY = { duration: CLOSE_S };
const INSTANT = { duration: 0 };

/**
 * A panel that mounts when it opens. Use it where the content holds no state
 * worth keeping, such as a step's result or a burst of step rows. ``history``
 * gives the turn's fold its own motion.
 */
export function Reveal({
  open,
  history = false,
  children,
}: {
  open: boolean;
  history?: boolean;
  children: React.ReactNode;
}) {
  const reduce = Boolean(useReducedMotion());
  const shut =
    history && !reduce ? { height: 0, opacity: 0, y: -RISE_PX } : { height: 0, opacity: 0 };
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          initial={reduce ? { opacity: 0 } : shut}
          animate={{ height: "auto", opacity: 1, y: 0 }}
          // The exit carries its own timing, which runs before the unmount.
          exit={
            reduce
              ? { opacity: 0, transition: history ? REDUCED_HISTORY : INSTANT }
              : { ...shut, transition: history ? HISTORY_CLOSE : DISCLOSURE }
          }
          transition={reduce ? (history ? REDUCED_HISTORY : INSTANT) : DISCLOSURE}
          className="overflow-hidden"
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
