"use client";

import * as React from "react";
import { motion } from "framer-motion";

import { formatMsg, msg } from "@/shared/lib/messages";
import { Button } from "@/shared/ui/primitives/button";

import { secondsLeft } from "./capacity-retry";

/**
 * Codex's capacity retry: an outline button that counts down, fills as the
 * wait passes and retries by itself at zero. A click retries at once. With no
 * wait it is a plain Retry button.
 */
export function RetryCountdownButton({
  seconds,
  onRetry,
  disabled,
}: {
  /** The wait before the automatic retry; null shows a plain Retry button. */
  seconds: number | null;
  onRetry: () => void;
  disabled?: boolean;
}) {
  const [deadline] = React.useState(() => (seconds === null ? null : Date.now() + seconds * 1000));
  const [left, setLeft] = React.useState(seconds);
  const retry = React.useEffectEvent(onRetry);

  React.useEffect(() => {
    if (deadline === null || disabled) return;
    const id = window.setInterval(() => {
      const next = secondsLeft(deadline, Date.now());
      setLeft(next);
      if (next === 0) {
        window.clearInterval(id);
        retry();
      }
    }, 1000);
    return () => window.clearInterval(id);
  }, [deadline, disabled]);

  const counting = seconds !== null && left !== null && !disabled;
  return (
    <Button type="button" variant="outline" size="xs" className="relative overflow-hidden" disabled={disabled} onClick={onRetry}>
      {counting ? (
        <>
          <motion.span
            aria-hidden
            className="pointer-events-none absolute inset-0 origin-left bg-primary/15 rtl:origin-right"
            initial={false}
            animate={{ scaleX: (seconds - left) / seconds }}
          />
          <span className="relative z-10 tabular-nums">{formatMsg("shared.agent.retry_in", { seconds: left })}</span>
        </>
      ) : (
        msg("shared.agent.retry")
      )}
    </Button>
  );
}
