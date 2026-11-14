"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import { formatMsg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";

// A placeholder that splits the sentence, so the rolling number sits where
// each locale puts {progress}.
const TOKEN = "\u0000";

/**
 * The live line while the chat stream reconnects, as "Reconnecting 2/5".
 * The only motion is the attempt number rolling up; no spinner, no shimmer.
 */
export function ReconnectingNotice({ attempt, maxAttempts }: { attempt: number; maxAttempts: number }) {
  const reduceMotion = useReducedMotion();
  const count = new Intl.NumberFormat(getActiveIntlLocale());
  const sentence = formatMsg("shared.agent.reconnecting", { progress: TOKEN });
  const [before, after = ""] = sentence.split(TOKEN);
  return (
    <div
      role="status"
      className="flex w-full min-w-0 items-center gap-2 py-1 text-[0.8125rem] text-muted-foreground"
    >
      {/* Screen readers get the settled sentence; the rolling copy briefly holds two numbers. */}
      <span className="sr-only">
        {sentence.replace(TOKEN, `${count.format(attempt)}/${count.format(maxAttempts)}`)}
      </span>
      <span aria-hidden className="min-w-0 truncate text-muted-foreground/80">
        {before}
        {/* An LTR isolate keeps "2/5" in order inside a right-to-left sentence. */}
        <bdi dir="ltr" className="inline-flex items-baseline tabular-nums">
          <span className="relative inline-flex overflow-hidden">
            <AnimatePresence initial={false} mode="popLayout">
              <motion.span
                key={attempt}
                initial={reduceMotion ? false : { y: "100%", opacity: 0 }}
                animate={{ y: 0, opacity: 1 }}
                exit={reduceMotion ? { opacity: 0, transition: { duration: 0 } } : { y: "-100%", opacity: 0 }}
                transition={{ duration: 0.2, ease: [0.23, 1, 0.32, 1] }}
              >
                {count.format(attempt)}
              </motion.span>
            </AnimatePresence>
          </span>
          /{count.format(maxAttempts)}
        </bdi>
        {after}
      </span>
    </div>
  );
}
