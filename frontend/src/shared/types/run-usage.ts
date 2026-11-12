/** Shape of `GET /optimizations/{id}/usage`, the run page's Usage and cost tab. */

export type UsageRole =
  | "task"
  | "reflection"
  | "proposer"
  | "scorer"
  | "sandbox"
  | "setup"
  | "rounding"
  | "other";

/**
 * How a row was paid: through Skynet, through the owner's key with a fee, straight to the provider,
 * or by the owner's CLI subscription (local build).
 */
export type UsageBilling = "skynet" | "byok" | "direct" | "subscription";

/** One row of `GET /optimizations/{id}/usage`. */
export interface RunUsageRow {
  role: UsageRole | string;
  model: string | null;
  stage: string | null;
  candidate: string | null;
  // What an engine that makes several sorts of model calls used this one for (ShinkaEvolve).
  kind?: string | null;
  billing: UsageBilling | string;
  charged_cents: number;
  provider_cents: number | null;
  // What a subscription row's tokens would cost at API rates; display only, never billed.
  api_rate_cents?: number | null;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  latency_ms_total: number;
  latency_calls: number;
  unpriced_calls: number;
  pending_calls: number;
}

export interface RunUsage {
  rows: RunUsageRow[];
  settling: boolean;
  proposer: boolean;
}
