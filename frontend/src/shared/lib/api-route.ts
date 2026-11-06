// Import-free so `node --test` can load it without the `@/` path alias.

/** Which backend answers a call: the Mac's own API, or the linked hosted account through it. */
export type ApiSource = "local" | "hosted";

/** Local API route that forwards a call to the linked hosted account. */
export const HOSTED_PROXY_PREFIX = "/local/hosted";

/** The path to send to the local API base for `source`. */
export function apiPath(path: string, source: ApiSource = "local"): string {
  return source === "hosted" ? `${HOSTED_PROXY_PREFIX}${path}` : path;
}

/** Whether a routed path (or full URL) goes through the hosted proxy. */
export function isHostedPath(pathOrUrl: string): boolean {
  return pathOrUrl.includes(`${HOSTED_PROXY_PREFIX}/`);
}

/** Whether hosted runs can be read: only the Mac app (local mode) with a linked account. */
export function hostedAvailable(localMode: boolean, linked: boolean): boolean {
  return localMode && linked;
}

/**
 * The source a call really goes to. Hosted needs local mode and a linked
 * account; otherwise the call stays local, so the web app never changes and
 * an unlinked Mac never gets the proxy's 409.
 */
export function resolveSource(
  requested: ApiSource | undefined,
  localMode: boolean,
  linked: boolean,
): ApiSource {
  return requested === "hosted" && hostedAvailable(localMode, linked) ? "hosted" : "local";
}

/** Read the `?source=` query value of a run page. */
export function sourceFromParam(value: string | null | undefined): ApiSource {
  return value === "hosted" ? "hosted" : "local";
}

/** The app path of a run page; a hosted run carries `?source=hosted`. */
export function runHref(optimizationId: string, source: ApiSource = "local"): string {
  return source === "hosted"
    ? `/optimizations/${optimizationId}?source=hosted`
    : `/optimizations/${optimizationId}`;
}

/** The run page of one grid pair (or the run itself when `pairIndex` is null), keeping the source. */
export function pairHref(
  optimizationId: string,
  pairIndex: number | null,
  source: ApiSource = "local",
): string {
  const base = runHref(optimizationId, source);
  if (pairIndex == null) return base;
  return `${base}${base.includes("?") ? "&" : "?"}pair=${pairIndex}`;
}

/** The prefix of a hosted item's key; a local item keeps its bare id. */
const HOSTED_KEY_PREFIX = "hosted:";

/**
 * One key for an item that can come from either source, so a hosted ID never
 * collides with a local one in a merged list. A local ID stays bare, so local
 * links, stored read marks and the agent stream keep working unchanged.
 */
export function sourceKey(source: ApiSource, id: string): string {
  return source === "hosted" ? `${HOSTED_KEY_PREFIX}${id}` : id;
}

/** Split a {@link sourceKey} into its source and the ID that source knows. */
export function parseSourceKey(key: string): { source: ApiSource; id: string } {
  return key.startsWith(HOSTED_KEY_PREFIX)
    ? { source: "hosted", id: key.slice(HOSTED_KEY_PREFIX.length) }
    : { source: "local", id: key };
}

/**
 * Hosted routes that refuse the Mac's link token: keys, payments, protected
 * agent settings and whole-account actions. The Mac never sends these through
 * the hosted proxy, even when a caller asks for the hosted source.
 */
export const HOSTED_CLOSED_PATHS: readonly string[] = [
  "/billing/byok/keys",
  "/billing/payment-methods",
  "/billing/checkout",
  "/billing/portal",
  "/billing/subscription",
  "/agent/protected-settings",
  "/account/delete",
  "/account/export",
];

/** Whether `path` may go through the hosted proxy: it is not one of {@link HOSTED_CLOSED_PATHS}. */
export function hostedRouteOpen(path: string): boolean {
  const bare = path.split("?", 1)[0] ?? path;
  return !HOSTED_CLOSED_PATHS.some((closed) => bare === closed || bare.startsWith(`${closed}/`));
}
