"use client";

import * as React from "react";
import { motion } from "framer-motion";

import { formatMsg } from "@/shared/lib/messages";
import { Button } from "@/shared/ui/primitives/button";

import { secondsLeft } from "./capacity-retry";

/**
 * The capacity retry: an outline button that counts down, fills as the wait
 * passes and retries by itself at zero. A click retries at once. Give it a new
 * key for each failure, so the countdown starts again.
 */
export function RetryCountdownButton({ seconds, onRetry }: { seconds: number; onRetry: () => void }) {
  const [deadline] = React.useState(() => Date.now() + seconds * 1000);
  const [left, setLeft] = React.useState(seconds);
  const retry = React.useEffectEvent(onRetry);

  React.useEffect(() => {
    const id = window.setInterval(() => {
      const next = secondsLeft(deadline, Date.now());
      setLeft(next);
      if (next === 0) {
        window.clearInterval(id);
        retry();
      }
    }, 1000);
    return () => window.clearInterval(id);
  }, [deadline]);

  return (
    <Button
      type="button"
      variant="outline"
      size="xs"
      className="relative shrink-0 overflow-hidden"
      onClick={onRetry}
    >
      <motion.span
        aria-hidden
        className="pointer-events-none absolute inset-0 origin-left bg-primary/15 rtl:origin-right"
        initial={false}
        animate={{ scaleX: (seconds - left) / seconds }}
      />
      <span className="relative z-10 tabular-nums">{formatMsg("shared.agent.retry_in", { seconds: left })}</span>
    </Button>
  );
}
