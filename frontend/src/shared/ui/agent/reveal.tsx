"use client";

import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

/**
 * One motion for everything an agent turn opens: a burst of steps and a
 * step's own result. The panel unrolls from under its header while its
 * content settles down into place.
 */

// An exponential ease-out arrives with confidence; the close uses an ease-in
// so dismissal reads as crisp instead of slow.
const OPEN_EASE = [0.16, 1, 0.3, 1] as const;
const CLOSE_EASE = [0.4, 0, 1, 1] as const;

const OPEN_S = 0.2;
const CLOSE_S = 0.14;
const RISE_PX = 2;

const ENTER = {
  height: { duration: OPEN_S, ease: OPEN_EASE },
  // The content follows the height a touch late, so the panel reads
  // as unrolling instead of two things moving at once.
  opacity: { duration: OPEN_S * 0.8, delay: OPEN_S * 0.15, ease: "linear" as const },
  y: { duration: OPEN_S, ease: OPEN_EASE },
};
const LEAVE = {
  height: { duration: CLOSE_S, ease: CLOSE_EASE },
  opacity: { duration: CLOSE_S * 0.7, ease: "linear" as const },
  y: { duration: 0 },
};
const INSTANT = { duration: 0 };

/**
 * A panel that mounts when it opens. Use it where the content holds no state
 * worth keeping, such as a step's result or a burst of step rows.
 */
export function Reveal({ open, children }: { open: boolean; children: React.ReactNode }) {
  const reduce = Boolean(useReducedMotion());
  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          initial={reduce ? { opacity: 0 } : { height: 0, opacity: 0, y: -RISE_PX }}
          animate={{ height: "auto", opacity: 1, y: 0 }}
          // The exit carries its own timing, which runs before the unmount.
          exit={
            reduce
              ? { opacity: 0, transition: INSTANT }
              : { height: 0, opacity: 0, transition: LEAVE }
          }
          transition={reduce ? INSTANT : ENTER}
          className="overflow-hidden"
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
