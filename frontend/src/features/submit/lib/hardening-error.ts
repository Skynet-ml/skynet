/**
 * Catalog keys for the error codes a failed hardening job stores in `job.error`.
 * The worker's hardening runner sets these codes (`backend/core/worker/hardening_runner.py`).
 */
const HARDENING_ERROR_KEYS = new Map<string, string>([
  ["hardening.no_scorer", "submit.v2.harden.error.no_scorer"],
  ["hardening.no_target", "submit.v2.harden.error.no_target"],
  ["hardening.no_python_metric", "submit.v2.harden.error.no_python_metric"],
  ["hardening.no_managed_models", "submit.v2.harden.error.no_managed_models"],
  ["hardening.byok_missing", "submit.v2.harden.error.byok_missing"],
  ["hardening.byok_not_served", "submit.v2.harden.error.byok_not_served"],
  ["hardening.runner_failed", "submit.v2.harden.error.runner_failed"],
]);

/** Shown for any other value: a failed pass can also store an exception's message or type name. */
export const HARDENING_ERROR_UNKNOWN_KEY = "submit.v2.harden.error.unknown";

/** The catalog key of the sentence to show for a failed job's error. */
export function hardeningErrorKey(error: string): string {
  return HARDENING_ERROR_KEYS.get(error) ?? HARDENING_ERROR_UNKNOWN_KEY;
}
