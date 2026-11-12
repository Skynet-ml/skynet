"use client";

import type { OptimizationPayloadResponse, OptimizationStatusResponse } from "@/shared/types/api";
import { BlackboxConfigCard } from "./BlackboxConfig";

/**
 * Every setting the run was submitted with. Nothing here is gated on advanced
 * mode: the tab is the record of what actually ran.
 */
export function ConfigTab({
  job,
  payload,
}: {
  job: OptimizationStatusResponse;
  payload: OptimizationPayloadResponse | null;
}) {
  return (
    <BlackboxConfigCard job={job} payload={(payload?.payload ?? {}) as Record<string, unknown>} />
  );
}
