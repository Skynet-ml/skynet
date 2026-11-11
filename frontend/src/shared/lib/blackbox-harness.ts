import { msg } from "@/shared/lib/messages";
import type { BlackboxHarness, ModelConfig } from "@/shared/types/api";

// The backend's BLACKBOX_HARNESSES in the same order, minus "custom": the
// wizard only offers the built-in harnesses, though the API still accepts it.
export const BLACKBOX_HARNESSES: readonly BlackboxHarness[] = [
  "pi",
  "codex",
  "opencode",
  "opencode_v2",
  "prime",
  "aider",
  "openhands",
  "openhands_sdk",
  "antigravity_sdk",
  "goose",
  "hermes",
  "eve",
  "deerflow",
  "cline",
  "code_puppy",
];

// Each harness module's `supported_efforts` in the backend. A harness that is
// not listed declares none, which the backend reads as "every level".
const HARNESS_EFFORTS: Partial<Record<BlackboxHarness, readonly string[]>> = {
  pi: ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
  codex: ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
  opencode_v2: ["low", "medium", "high"],
  prime: ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
  openhands: ["none", "minimal", "low", "medium", "high", "xhigh"],
  openhands_sdk: ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
  antigravity_sdk: ["minimal", "low", "medium", "high", "xhigh"],
  hermes: ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
  eve: ["none", "minimal", "low", "medium", "high", "xhigh"],
  cline: ["none", "low", "medium", "high", "xhigh"],
  code_puppy: ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
};

/** The effort levels a harness accepts; `null` when it accepts every level. */
export function harnessEfforts(harness: BlackboxHarness): readonly string[] | null {
  return HARNESS_EFFORTS[harness] ?? null;
}

/**
 * The config without a reasoning effort the harness refuses, so the run keeps
 * the harness default instead of failing at submit.
 */
export function keepHarnessEffort(config: ModelConfig, harness: BlackboxHarness): ModelConfig {
  const levels = harnessEfforts(harness);
  const effort = config.extra?.reasoning_effort;
  if (!levels || typeof effort !== "string" || levels.includes(effort)) return config;
  const { reasoning_effort: _dropped, ...extra } = config.extra ?? {};
  return { ...config, extra: Object.keys(extra).length ? extra : undefined };
}

// Stored runs may name a harness Skynet no longer offers (claude_code); those
// show their raw id instead of a catalog label.
export function harnessLabel(harness: string): string {
  return harness === "custom" || BLACKBOX_HARNESSES.includes(harness as BlackboxHarness)
    ? msg(`submit.blackbox.start.harness.${harness as BlackboxHarness}`)
    : harness;
}
