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
  return gateFailures(err).flatMap(({ key, params }) => {
    const text = translate(key, params);
    return text ? [text] : [];
  });
}

/** The setup check keys that refuse a secret for a host it may not go to. */
const SECRET_HOST_KEYS = new Set(["repo_gate.secret_host_unlisted", "repo_gate.secret_host_not_allowed"]);

/**
 * The fix messages for secrets the setup check refused for a host, keyed by
 * the secret's name, so the form can show each one on its secret's row.
 */
export function secretHostIssues(err: unknown, translate: Translate): Record<string, string> {
  const issues: Record<string, string> = {};
  for (const { key, params } of gateFailures(err)) {
    const secret = params?.secret;
    if (!SECRET_HOST_KEYS.has(key) || typeof secret !== "string") continue;
    const text = translate(key, params);
    if (text) issues[secret] = issues[secret] ? `${issues[secret]} ${text}` : text;
  }
  return issues;
}

/** The well-formed `{ key, params }` entries of a refused setup check. */
function gateFailures(err: unknown): Array<{ key: string; params?: Record<string, unknown> }> {
  if (typeof err !== "object" || err === null) return [];
  const { code, params } = err as { code?: unknown; params?: Record<string, unknown> };
  const failures = params?.failures;
  if (code !== VALIDATION_FAILED_CODE || !Array.isArray(failures)) return [];
  return failures.flatMap((failure: unknown) => {
    if (typeof failure !== "object" || failure === null) return [];
    const { key, params: values } = failure as { key?: unknown; params?: unknown };
    if (typeof key !== "string") return [];
    return [
      {
        key,
        params:
          typeof values === "object" && values !== null
            ? (values as Record<string, unknown>)
            : undefined,
      },
    ];
  });
}
