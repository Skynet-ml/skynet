import assert from "node:assert/strict";
import test from "node:test";

import {
  errorMessageKey,
  parseMcpRequest,
  returnHost,
  safeRedirect,
  scopeMessageKey,
} from "./consent.ts";

const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";

function query(overrides: Record<string, string | null> = {}): URLSearchParams {
  const values: Record<string, string | null> = {
    client_id: "https://claude.ai/oauth/mcp-oauth-client-metadata",
    redirect_uri: "https://claude.ai/api/mcp/auth_callback",
    code_challenge: CHALLENGE,
    code_challenge_method: "S256",
    scope: "runs:read runs:write",
    state: "st",
    resource: "https://mcp.skynetml.com/mcp",
    ...overrides,
  };
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== null) params.set(key, value);
  return params;
}

test("parses a complete request in backend scope order", () => {
  const parsed = parseMcpRequest(query({ scope: "runs:write runs:read" }));
  assert.ok(parsed.ok);
  assert.deepEqual(parsed.request.scopes, ["runs:read", "runs:write"]);
  assert.equal(parsed.request.state, "st");
  assert.equal(parsed.request.resource, "https://mcp.skynetml.com/mcp");
});

test("an empty scope asks for every scope", () => {
  const parsed = parseMcpRequest(query({ scope: null }));
  assert.ok(parsed.ok);
  assert.deepEqual(parsed.request.scopes, ["runs:read", "runs:write"]);
});

test("refuses incomplete or unsafe requests", () => {
  for (const overrides of [
    { client_id: null },
    { redirect_uri: "javascript:alert(1)" },
    { redirect_uri: null },
    { code_challenge_method: "plain" },
    { code_challenge: "short" },
  ]) {
    assert.deepEqual(
      parseMcpRequest(query(overrides)),
      { ok: false, problem: "invalid_request" },
      JSON.stringify(overrides),
    );
  }
  assert.deepEqual(parseMcpRequest(query({ scope: "admin" })), {
    ok: false,
    problem: "invalid_scope",
  });
});

test("follows only http(s) redirects", () => {
  assert.equal(safeRedirect("https://claude.ai/cb?code=1"), "https://claude.ai/cb?code=1");
  assert.equal(safeRedirect("http://127.0.0.1:5000/cb"), "http://127.0.0.1:5000/cb");
  assert.equal(safeRedirect("javascript:alert(1)"), null);
  assert.equal(safeRedirect("data:text/html,x"), null);
  assert.equal(safeRedirect("/relative"), null);
});

test("maps scopes and errors to message keys", () => {
  assert.equal(scopeMessageKey("runs:write"), "mcp_consent.scope.runs_write");
  assert.equal(errorMessageKey("mcp.invalid_redirect"), "mcp_consent.error.invalid_request");
  assert.equal(errorMessageKey(undefined, 429), "mcp_consent.error.rate_limit");
  assert.equal(errorMessageKey("boom"), "mcp_consent.error.generic");
});

test("the return host is the real redirect host, also for a metadata-document client", () => {
  assert.equal(
    returnHost({ client_host: "claude.ai", redirect_host: "localhost:56369" }),
    "localhost:56369",
  );
  assert.equal(returnHost({ client_host: null, redirect_host: "claude.ai" }), "claude.ai");
});
