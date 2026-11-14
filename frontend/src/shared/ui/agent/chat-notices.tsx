"use client";

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { Cube, Info } from "@/shared/ui/icons";
import { TooltipButton } from "@/shared/ui/tooltip-button";

// Both rows use the step row's quiet line, as Codex draws them as inline status items in the transcript.
const LINE = "flex w-full min-w-0 items-center gap-2 py-1 text-[0.8125rem] text-muted-foreground";

/** The line before a turn that runs on another model than the turn before it; its info icon warns about the switch. */
export function ModelChangedNotice({ fromModel, toModel }: { fromModel: string; toModel: string }) {
  return (
    <div className={LINE}>
      <Cube className="size-3.5 shrink-0" aria-hidden />
      <span dir="auto" className="min-w-0 truncate">
        {formatMsg("shared.agent.model_changed", { from: fromModel, to: toModel })}
      </span>
      <TooltipButton
        side="top"
        align="center"
        tooltip={
          <span className="flex max-w-64 flex-col text-center">
            <span>{msg("shared.agent.model_changed_warning_1")}</span>
            <span>{msg("shared.agent.model_changed_warning_2")}</span>
          </span>
        }
      >
        <span
          role="img"
          tabIndex={0}
          aria-label={msg("shared.agent.model_changed_warning_label")}
          className="inline-flex size-5 shrink-0 items-center justify-center rounded-full outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Info className="size-3" aria-hidden />
        </span>
      </TooltipButton>
    </div>
  );
}

/**
 * The live line while a dropped response stream reconnects, as "Reconnecting 2/5".
 * `busy` says the server was overloaded, so the line says why it waits.
 * As in Codex, the only motion is the attempt number rolling up; no spinner, no shimmer.
 */
export function ReconnectingNotice({
  attempt,
  maxAttempts,
  busy = false,
}: {
  attempt: number | null;
  maxAttempts: number | null;
  busy?: boolean;
}) {
  const reduceMotion = useReducedMotion();
  const count = new Intl.NumberFormat(getActiveIntlLocale());
  const hasProgress = attempt != null && maxAttempts != null;
  // A placeholder token splits the sentence, so the rolling number sits where each locale puts {progress}.
  const TOKEN = "\u0000";
  const sentence = busy
    ? hasProgress
      ? formatMsg("shared.agent.server_busy_progress", { progress: TOKEN })
      : msg("shared.agent.server_busy")
    : formatMsg("shared.agent.reconnecting", { progress: hasProgress ? TOKEN : "" }).trim();
  const [before, after = ""] = sentence.split(TOKEN);
  return (
    <div role="status" className={LINE}>
      {/* Screen readers get the settled sentence; the rolling copy briefly holds two numbers. */}
      <span className="sr-only">{hasProgress ? sentence.replace(TOKEN, `${count.format(attempt)}/${count.format(maxAttempts)}`) : sentence}</span>
      <span aria-hidden className="min-w-0 truncate text-muted-foreground/80">
        {before}
        {hasProgress && (
          // An LTR isolate keeps "2/5" in order inside a right-to-left sentence.
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
        )}
        {after}
      </span>
    </div>
  );
}
