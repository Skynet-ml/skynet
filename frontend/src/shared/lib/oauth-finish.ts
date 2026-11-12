/**
 * Read the OAuth sign-in result that a backend callback puts in the URL
 * fragment. The fragment never goes to a server or into a Referer header. The
 * signed-in app sends it to the flow's finish route, and the backend links the
 * account only when this user started the sign-in.
 */

export interface OAuthFinish {
  /** ``mcp``, ``openrouter`` or ``connector:<provider>``. */
  flow: string;
  state: string;
  code: string;
  /** The backend route that finishes this flow. */
  path: string;
}

// A slug is spliced into the finish path, so only plain provider slugs pass.
const CONNECTOR_FLOW = /^connector:([a-z_]+)$/;

/** Return the finish route for a flow, or null for a flow the app does not know. */
export function oauthFinishPath(flow: string): string | null {
  if (flow === "mcp") return "/mcp/connections/finish";
  if (flow === "openrouter") return "/billing/byok/openrouter/oauth/finish";
  const connector = CONNECTOR_FLOW.exec(flow);
  return connector ? `/connectors/${connector[1]}/oauth/finish` : null;
}

/**
 * Return the flow, state and code from a
 * ``#oauth_flow=..&oauth_state=..&oauth_code=..`` hash, or null when a value
 * is missing or the flow is unknown.
 */
export function parseOAuthFinish(hash: string): OAuthFinish | null {
  const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  const flow = params.get("oauth_flow");
  const state = params.get("oauth_state");
  const code = params.get("oauth_code");
  const path = flow ? oauthFinishPath(flow) : null;
  return flow && state && code && path ? { flow, state, code, path } : null;
}
