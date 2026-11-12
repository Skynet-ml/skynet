import assert from "node:assert/strict";
import test from "node:test";

import {
  approvedCallbackUrl,
  callbackUrl,
  deniedCallbackUrl,
  errorMessageKey,
  isAppCallback,
  isLoopbackCallback,
  parseLinkRequest,
  scopeMessageKey,
} from "./consent.ts";

const CALLBACK = "http://127.0.0.1:51234/local/hosted-link/callback";
const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
const STATE = "gAAAAABn_state+with/odd=chars==";

// The query the Mac app builds with Python's urlencode (spaces become "+").
function macQuery(overrides: Record<string, string | null> = {}): URLSearchParams {
  const values: Record<string, string | null> = {
    response_type: "code",
    client_id: "skynet-desktop",
    redirect_uri: CALLBACK,
    code_challenge: CHALLENGE,
    code_challenge_method: "S256",
    state: STATE,
    scope: "identity balance:read sync models connectors web",
    device_name: "Gilad's MacBook Pro",
    ...overrides,
  };
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== null) query.set(key, value);
  return new URLSearchParams(query.toString());
}

test("loopback callback accepts only the Mac app's callback on 127.0.0.1 or [::1]", () => {
  assert.ok(isLoopbackCallback(CALLBACK));
  assert.ok(isLoopbackCallback("http://[::1]:1024/local/hosted-link/callback"));
  assert.ok(isLoopbackCallback("http://127.0.0.1:65535/local/hosted-link/callback"));
  for (const uri of [
    "https://127.0.0.1:51234/local/hosted-link/callback",
    "http://localhost:51234/local/hosted-link/callback",
    "http://127.0.0.1/local/hosted-link/callback",
    "http://127.0.0.1:80/local/hosted-link/callback",
    "http://127.0.0.1:1023/local/hosted-link/callback",
    "http://127.0.0.1:65536/local/hosted-link/callback",
    "http://127.0.0.1:051234/local/hosted-link/callback",
    "http://127.0.0.1:51234/local/hosted-link/callback/",
    "http://127.0.0.1:51234/local/hosted-link/callback?x=1",
    "http://127.0.0.1:51234/other",
    "http://127.0.0.1.evil.com:51234/local/hosted-link/callback",
    "http://evil.com@127.0.0.1:51234/local/hosted-link/callback",
    "http://127.0.0.1:51234@evil.com/local/hosted-link/callback",
    "javascript:alert(1)//127.0.0.1:51234/local/hosted-link/callback",
    "",
  ]) {
    assert.equal(isLoopbackCallback(uri), false, uri);
  }
});

test("app callback accepts only the exact skynet://link/callback URL or a loopback callback", () => {
  assert.ok(isAppCallback("skynet://link/callback"));
  assert.ok(isAppCallback(CALLBACK));
  for (const uri of [
    "skynet://link/callback/",
    "skynet://link/callback?x=1",
    "skynet://link/other",
    "skynet://evil/callback",
    "skynet:link/callback",
    "SKYNET://link/callback",
    "skynet-evil://link/callback",
    "https://evil.com/cb",
    "",
  ]) {
    assert.equal(isAppCallback(uri), false, uri);
  }
});

test("approve and cancel hand the result to the skynet:// URL", () => {
  const parsed = parseLinkRequest(macQuery({ redirect_uri: "skynet://link/callback" }));
  assert.ok(parsed.ok);
  const approved = new URL(approvedCallbackUrl(parsed.request, "abc") ?? "");
  assert.equal(`${approved.protocol}//${approved.host}${approved.pathname}`, "skynet://link/callback");
  assert.equal(approved.searchParams.get("code"), "abc");
  assert.equal(approved.searchParams.get("state"), STATE);
  assert.equal(
    deniedCallbackUrl(parsed.request),
    `skynet://link/callback?error=access_denied&state=${encodeURIComponent(STATE)}`,
  );
});

test("a valid Mac request parses, with scopes in backend order", () => {
  const parsed = parseLinkRequest(macQuery({ scope: "web identity" }));
  assert.deepEqual(parsed, {
    ok: true,
    request: {
      redirectUri: CALLBACK,
      state: STATE,
      codeChallenge: CHALLENGE,
      scopes: ["identity", "web"],
      deviceName: "Gilad's MacBook Pro",
    },
  });
});

test("the full scope set from the Mac app parses", () => {
  const parsed = parseLinkRequest(macQuery());
  assert.ok(parsed.ok);
  assert.deepEqual(parsed.request.scopes, [
    "identity",
    "balance:read",
    "sync",
    "models",
    "connectors",
    "web",
  ]);
});

test("a foreign redirect is refused before any other check", () => {
  assert.deepEqual(parseLinkRequest(macQuery({ redirect_uri: "https://evil.com/cb", state: null })), {
    ok: false,
    problem: "invalid_redirect",
  });
  assert.deepEqual(parseLinkRequest(macQuery({ redirect_uri: null })), {
    ok: false,
    problem: "invalid_redirect",
  });
});

test("incomplete or wrong requests are refused", () => {
  for (const overrides of [
    { response_type: "token" },
    { client_id: "other" },
    { code_challenge_method: "plain" },
    { code_challenge: "short" },
    { code_challenge: null },
    { state: null },
    { device_name: "   " },
    { device_name: "x".repeat(129) },
  ]) {
    assert.deepEqual(
      parseLinkRequest(macQuery(overrides)),
      { ok: false, problem: "invalid_request" },
      JSON.stringify(overrides),
    );
  }
});

test("unknown, empty or badly spaced scopes are refused", () => {
  for (const scope of ["identity admin", "", "identity  web", " identity"]) {
    assert.deepEqual(parseLinkRequest(macQuery({ scope })), { ok: false, problem: "invalid_scope" }, scope);
  }
});

test("approve sends the code and the state back to the Mac, URL-encoded", () => {
  const parsed = parseLinkRequest(macQuery());
  assert.ok(parsed.ok);
  const url = approvedCallbackUrl(parsed.request, "abc_-123");
  assert.ok(url);
  assert.ok(url.startsWith(`${CALLBACK}?`));
  const back = new URL(url);
  assert.equal(back.origin + back.pathname, CALLBACK);
  assert.equal(back.searchParams.get("code"), "abc_-123");
  assert.equal(back.searchParams.get("state"), STATE);
  assert.equal(back.searchParams.get("error"), null);
  assert.ok(!url.includes("+with/"), "state must be encoded");
});

test("cancel sends access_denied and the state back to the Mac", () => {
  const parsed = parseLinkRequest(macQuery({ redirect_uri: "http://[::1]:40000/local/hosted-link/callback" }));
  assert.ok(parsed.ok);
  const url = deniedCallbackUrl(parsed.request);
  assert.equal(
    url,
    `http://[::1]:40000/local/hosted-link/callback?error=access_denied&state=${encodeURIComponent(STATE)}`,
  );
});

test("no callback URL is built for a non-loopback redirect", () => {
  assert.equal(callbackUrl("https://evil.com/local/hosted-link/callback", { code: "x" }), null);
  const forged = {
    redirectUri: "https://evil.com/cb",
    state: "s",
    codeChallenge: CHALLENGE,
    scopes: [],
    deviceName: "Mac",
  };
  assert.equal(approvedCallbackUrl(forged, "code"), null);
  assert.equal(deniedCallbackUrl(forged), null);
});

test("scope lines and error codes map to message keys", () => {
  assert.equal(scopeMessageKey("balance:read"), "desktop_link.scope.balance_read");
  assert.equal(scopeMessageKey("identity"), "desktop_link.scope.identity");
  assert.equal(errorMessageKey("desktop.too_many_links", 409), "desktop_link.error.too_many_links");
  assert.equal(errorMessageKey("desktop.invalid_redirect", 400), "desktop_link.error.invalid_redirect");
  assert.equal(errorMessageKey("invalid_redirect"), "desktop_link.error.invalid_redirect");
  assert.equal(errorMessageKey("desktop.invalid_scope", 400), "desktop_link.error.invalid_scope");
  assert.equal(errorMessageKey("desktop.invalid_device_name", 400), "desktop_link.error.invalid_request");
  assert.equal(errorMessageKey("rate_limit.exceeded", 429), "desktop_link.error.rate_limit");
  assert.equal(errorMessageKey(undefined, 429), "desktop_link.error.rate_limit");
  assert.equal(errorMessageKey(undefined, 500), "desktop_link.error.generic");
  assert.equal(errorMessageKey("desktop.session_required", 403), "desktop_link.error.generic");
});
