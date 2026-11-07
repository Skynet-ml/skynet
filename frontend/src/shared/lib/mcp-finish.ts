/**
 * Read the MCP sign-in result that the backend callback puts in the URL
 * fragment. The fragment never goes to a server or into a Referer header, and
 * the signed-in app finishes the sign-in with it.
 */

export interface McpFinish {
  state: string;
  code: string;
}

/** Return the state and code from a ``#mcp_state=..&mcp_code=..`` hash, or null. */
export function parseMcpFinish(hash: string): McpFinish | null {
  const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  const state = params.get("mcp_state");
  const code = params.get("mcp_code");
  return state && code ? { state, code } : null;
}
