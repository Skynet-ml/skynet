/**
 * Request checks for the MCP consent page (SKY-229).
 *
 * The backend's ``/oauth/authorize`` sends the browser to ``/mcp/authorize``
 * after it checks the client, its redirect and the PKCE challenge. The page
 * checks the query again before it calls the backend, and it follows only an
 * http(s) redirect URL that the backend returns.
 *
 * Kept import-free so the Node unit test can load it directly.
 */

// Same order as MCP_SCOPES in backend/core/api/mcp_oauth.py.
export const MCP_SCOPES = ["runs:read", "runs:write"] as const;
export type McpScope = (typeof MCP_SCOPES)[number];

const CODE_CHALLENGE = /^[A-Za-z0-9_-]{43}$/;
const MAX_FIELD = 512;

export type McpRequest = {
  clientId: string;
  redirectUri: string;
  codeChallenge: string;
  /** Requested scopes, in backend order. */
  scopes: McpScope[];
  state: string | null;
  resource: string | null;
};

export type ParsedMcpRequest =
  | { ok: true; request: McpRequest }
  | { ok: false; problem: "invalid_request" | "invalid_scope" };

function isMcpScope(value: string): value is McpScope {
  return (MCP_SCOPES as readonly string[]).includes(value);
}

/** Check the query of a ``/mcp/authorize`` request and return the request. */
export function parseMcpRequest(params: URLSearchParams): ParsedMcpRequest {
  const clientId = params.get("client_id") ?? "";
  const redirectUri = params.get("redirect_uri") ?? "";
  const codeChallenge = params.get("code_challenge") ?? "";
  if (
    !clientId ||
    clientId.length > MAX_FIELD ||
    !isHttpUrl(redirectUri) ||
    redirectUri.length > MAX_FIELD ||
    params.get("code_challenge_method") !== "S256" ||
    !CODE_CHALLENGE.test(codeChallenge)
  ) {
    return { ok: false, problem: "invalid_request" };
  }
  const requested = (params.get("scope") ?? "").split(" ").filter(Boolean);
  if (requested.some((scope) => !isMcpScope(scope))) return { ok: false, problem: "invalid_scope" };
  const scopes = MCP_SCOPES.filter((scope) => requested.length === 0 || requested.includes(scope));
  return {
    ok: true,
    request: {
      clientId,
      redirectUri,
      codeChallenge,
      scopes,
      state: params.get("state"),
      resource: params.get("resource"),
    },
  };
}

/** Whether ``uri`` is an absolute http or https URL. */
export function isHttpUrl(uri: string): boolean {
  try {
    const url = new URL(uri);
    return url.protocol === "https:" || url.protocol === "http:";
  } catch {
    return false;
  }
}

/** The redirect URL to follow, or null when it is not http(s). */
export function safeRedirect(uri: string): string | null {
  return isHttpUrl(uri) ? uri : null;
}

/**
 * Host the browser returns to after approval. It is the security signal, so
 * it always comes from the real redirect URI, never from the client ID host.
 */
export function returnHost(client: { client_host: string | null; redirect_host: string }): string {
  return client.redirect_host;
}

/** Message key for one scope line on the consent card. */
export function scopeMessageKey(scope: McpScope): string {
  return `mcp_consent.scope.${scope.replace(":", "_")}`;
}

/** Message key for a request problem or a failed backend call. */
export function errorMessageKey(code: string | undefined, status?: number): string {
  switch (code) {
    case "invalid_scope":
    case "mcp.invalid_scope":
      return "mcp_consent.error.invalid_scope";
    case "invalid_request":
    case "mcp.invalid_request":
    case "mcp.invalid_client":
    case "mcp.invalid_redirect":
      return "mcp_consent.error.invalid_request";
    case "mcp.not_configured":
      return "mcp_consent.error.not_configured";
  }
  if (code === "rate_limit.exceeded" || status === 429) return "mcp_consent.error.rate_limit";
  return "mcp_consent.error.generic";
}
