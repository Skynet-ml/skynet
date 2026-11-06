import * as React from "react";

import { onLinkChange } from "@/shared/hooks/use-linked-account";

import { hostedRetryDelay } from "../lib/merge-runs";

/**
 * Reload a run list soon after its hosted page failed, and on each link
 * change (SKY-340). Without this the list waits for its next poll, so a cold
 * start shows "no runs" while hosted is slow or the link is still unknown.
 *
 * Call `failed` with each load's `hostedFailed`. Put `reloadKey` in the load
 * effect's deps: it changes when the list must load again. `pending` is true
 * while a retry is still to come, so the list shows loading, not empty.
 */
export function useHostedRetry(): {
  failed: (hostedFailed: boolean) => void;
  pending: boolean;
  reloadKey: number;
} {
  const [failures, setFailures] = React.useState(0);
  const [reloadKey, setReloadKey] = React.useState(0);

  React.useEffect(() => onLinkChange(() => setReloadKey((n) => n + 1)), []);

  React.useEffect(() => {
    const delay = hostedRetryDelay(failures);
    if (delay == null) return;
    const timer = setTimeout(() => setReloadKey((n) => n + 1), delay);
    return () => clearTimeout(timer);
  }, [failures]);

  const failed = React.useCallback(
    (hostedFailed: boolean) => setFailures((n) => (hostedFailed ? n + 1 : 0)),
    [],
  );
  return { failed, pending: hostedRetryDelay(failures) != null, reloadKey };
}
