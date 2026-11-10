"use client";

import * as React from "react";
import { fetchWithAuthRetry } from "@/shared/lib/api";
import { getRuntimeEnv, isLocalMode } from "@/shared/lib/runtime-env";
import type { CliUsageReport, CodexResetOutcome } from "../lib/cli-usage";

// The backend caches each CLI read for 30 s, so a faster poll only reads the cache.
const CLI_USAGE_POLL_MS = 30_000;

async function fetchCliUsage(): Promise<CliUsageReport> {
  const res = await fetchWithAuthRetry(`${getRuntimeEnv().apiUrl}/local/cli-usage`, {
    method: "GET",
  });
  if (!res.ok) throw new Error(`local cli usage ${res.status}`);
  return (await res.json()) as CliUsageReport;
}

/** Use one of the user's free Codex limit resets. */
export async function postCodexReset(): Promise<CodexResetOutcome> {
  const res = await fetchWithAuthRetry(`${getRuntimeEnv().apiUrl}/local/cli-usage/codex/reset`, {
    method: "POST",
  });
  if (!res.ok) return "error";
  const body = (await res.json()) as { outcome?: CodexResetOutcome };
  return body.outcome ?? "error";
}

/**
 * The local CLI usage report, read again every 30 s while the page is
 * visible, and at once when it becomes visible or the window gets focus. A
 * Mac app window behind another window stays "visible", so focus matters there. Hosted builds never call the
 * route: it exists only in local mode and answers 404 elsewhere. `reload`
 * reads it again at once, for example after a limit reset.
 */
export function useLocalCliUsage(): {
  report: CliUsageReport | null;
  loading: boolean;
  failed: boolean;
  reload: () => void;
} {
  const enabled = isLocalMode();
  const [report, setReport] = React.useState<CliUsageReport | null>(null);
  const [loading, setLoading] = React.useState(enabled);
  const [failed, setFailed] = React.useState(false);
  const request = React.useRef(0);

  const load = React.useCallback(() => {
    if (!enabled) return;
    const id = ++request.current;
    fetchCliUsage()
      .then((next) => {
        if (id !== request.current) return;
        setReport(next);
        setFailed(false);
      })
      .catch(() => {
        if (id === request.current) setFailed(true);
      })
      .finally(() => {
        if (id === request.current) setLoading(false);
      });
  }, [enabled]);

  React.useEffect(() => {
    if (!enabled) return;
    load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, CLI_USAGE_POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") load();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
      // Drop an answer that arrives after unmount.
      request.current += 1;
    };
  }, [enabled, load]);

  return { report, loading, failed, reload: load };
}
