"use client";

import * as React from "react";
import { fetchWithAuthRetry } from "@/shared/lib/api";
import { getRuntimeEnv, isLocalMode } from "@/shared/lib/runtime-env";
import type { CliUsageReport } from "../lib/cli-usage";

async function fetchCliUsage(): Promise<CliUsageReport> {
  const res = await fetchWithAuthRetry(`${getRuntimeEnv().apiUrl}/local/cli-usage`, {
    method: "GET",
  });
  if (!res.ok) throw new Error(`local cli usage ${res.status}`);
  return (await res.json()) as CliUsageReport;
}

/**
 * The local CLI usage report. Hosted builds never call the route: it exists
 * only in local mode and answers 404 elsewhere.
 */
export function useLocalCliUsage(): {
  report: CliUsageReport | null;
  loading: boolean;
  failed: boolean;
  refresh: () => void;
} {
  const enabled = isLocalMode();
  const [report, setReport] = React.useState<CliUsageReport | null>(null);
  const [loading, setLoading] = React.useState(enabled);
  const [failed, setFailed] = React.useState(false);
  const request = React.useRef(0);

  const refresh = React.useCallback(() => {
    if (!enabled) return;
    const id = ++request.current;
    setLoading(true);
    setFailed(false);
    fetchCliUsage()
      .then((next) => {
        if (id === request.current) setReport(next);
      })
      .catch(() => {
        if (id === request.current) setFailed(true);
      })
      .finally(() => {
        if (id === request.current) setLoading(false);
      });
  }, [enabled]);

  React.useEffect(() => {
    refresh();
  }, [refresh]);

  return { report, loading, failed, refresh };
}
