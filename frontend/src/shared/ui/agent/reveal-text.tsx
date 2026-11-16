"use client";

import * as React from "react";
import { PresenceContext, motion, useReducedMotion } from "framer-motion";

const WORD_STAGGER_S = 0.035;
const MAX_REVEAL_DELAY_S = 1;

// Lines still writing themselves in. A turn's end rebuilds its rows, and a
// line that landed just before that would otherwise show up already written.
const revealing = new Set<string>();

interface RevealTextProps {
  text: string;
  /** Write the line in; false for text that was already there (a reopened chat). */
  animate: boolean;
  className?: string;
}

/**
 * A line that writes itself in word by word, as streamed text does. Reduced
 * motion renders it at once; screen readers get the whole line once.
 */
export function RevealText({ text, animate, className }: RevealTextProps) {
  const reduce = useReducedMotion();
  const [writing] = React.useState(() => animate || revealing.has(text));
  const active = !reduce && writing;

  React.useEffect(() => {
    if (!active) return;
    revealing.add(text);
  }, [active, text]);

  if (!active) {
    return (
      <span dir="auto" className={className}>
        {text}
      </span>
    );
  }
  const parts = text.split(/(\s+)/);
  const lastWord = parts.findLastIndex((part) => !/^\s*$/.test(part));
  let word = 0;
  return (
    <span dir="auto" className={className}>
      <span className="sr-only">{text}</span>
      {/* A finished turn rebuilds its rows under AnimatePresence initial={false},
          which would show the words already written; they keep their own entrance. */}
      <PresenceContext.Provider value={null}>
        <span aria-hidden>
          {parts.map((part, i) =>
            /^\s*$/.test(part) ? (
              part
            ) : (
              <motion.span
                key={i}
                initial={{ opacity: 0, filter: "blur(3px)" }}
                animate={{ opacity: 1, filter: "blur(0px)" }}
                transition={{
                  duration: 0.28,
                  delay: Math.min(word++ * WORD_STAGGER_S, MAX_REVEAL_DELAY_S),
                  ease: [0.2, 0.8, 0.2, 1],
                }}
                onAnimationComplete={i === lastWord ? () => revealing.delete(text) : undefined}
              >
                {part}
              </motion.span>
            ),
          )}
        </span>
      </PresenceContext.Provider>
    </span>
  );
}
