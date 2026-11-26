"use client";

import { createPortal } from "react-dom";
import { motion, AnimatePresence, useReducedMotion } from "motion/react";

import { EvolutionMorph } from "@/shared/ui/evolution-morph";

/**
 * Hold (ms) the splash stays up before routing to the new optimization.
 * Mirrors the manual wizard submit so the agent-driven submit feels identical, and is
 * long enough for the evolution morph to land on the human (EVOLUTION_DURATION_MS).
 */
export const SUBMIT_SPLASH_HOLD_MS = 2200;

/**
 * Full-screen submit splash: the fish-to-human evolution morph over the warm canvas that
 * plays while an optimization submission settles and the app routes to the new
 * job. Shared by the manual wizard submit (``SubmitSplash``) and the
 * agent-panel auto-submit so both paths render the identical banner.
 *
 * `slideOut` makes hiding it the mirror of its entrance (the panel slides back
 * up, revealing what is beneath) instead of vanishing with the route change;
 * `onExited` fires once it is gone.
 */
export function SubmitSplashOverlay({
  show,
  slideOut = false,
  onExited,
}: {
  show: boolean;
  slideOut?: boolean;
  onExited?: () => void;
}) {
  const reduceMotion = useReducedMotion() ?? false;
  if (typeof document === "undefined") return null;

  return createPortal(
    <AnimatePresence onExitComplete={onExited}>
      {show && (
        <motion.div
          className="fixed inset-0 z-[99999] flex items-center justify-center"
          style={{ backgroundColor: "var(--surface-3)" }}
          initial={{ y: "-100%" }}
          animate={{ y: 0 }}
          exit={
            slideOut
              ? reduceMotion
                ? { opacity: 0, transition: { duration: 0.2 } }
                : { y: "-100%" }
              : undefined
          }
          transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        >
          <motion.div
            initial={{ scale: 0.8, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={{ delay: 0.3, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
          >
            <EvolutionMorph />
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}
