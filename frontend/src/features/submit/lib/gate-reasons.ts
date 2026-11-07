/** The 409 code `/blackbox/run` returns when the setup check, such as the repository gate, refuses a run. */
const VALIDATION_FAILED_CODE = "submission.validation_failed";

/** Word one catalog code in the active locale, or return undefined when the catalog has no entry. */
type Translate = (code: string, params?: Record<string, unknown>) => string | undefined;

/**
 * The reasons the setup check gave for refusing a run, each worded in the
 * active locale. The server sends them as `params.failures`, a list of
 * `{ key, params }` catalog entries (SKY-372). Returns an empty list for any
 * other error, or when no reason has catalog copy.
 */
export function gateReasons(err: unknown, translate: Translate): string[] {
  if (typeof err !== "object" || err === null) return [];
  const { code, params } = err as { code?: unknown; params?: Record<string, unknown> };
  const failures = params?.failures;
  if (code !== VALIDATION_FAILED_CODE || !Array.isArray(failures)) return [];
  return failures.flatMap((failure: unknown) => {
    if (typeof failure !== "object" || failure === null) return [];
    const { key, params: values } = failure as { key?: unknown; params?: unknown };
    if (typeof key !== "string") return [];
    const text = translate(
      key,
      typeof values === "object" && values !== null
        ? (values as Record<string, unknown>)
        : undefined,
    );
    return text ? [text] : [];
  });
}
