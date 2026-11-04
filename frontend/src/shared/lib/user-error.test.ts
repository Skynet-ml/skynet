import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { afterEach, beforeEach, test } from "node:test";

// The real fetch layer runs so the tests see the errors its helpers throw.
// Its own imports, the catalogs and the message lookup are stubbed.
const stubs: Record<string, string> = {
  "@/shared/lib/i18n": `const known = { "billing.insufficient_funds": "Add funds to continue.", "share.limit": "Limit is {n}.", "continuous.not_found": "That continuous plan was not found." };
  export const I18N_KEY = { USER_STORAGE_QUOTA_EXCEEDED: "user.storage_quota_exceeded", BILLING_INSUFFICIENT_FUNDS: "billing.insufficient_funds" };
  export const tI18n = (code, params) => known[code] ? known[code].replace("{n}", String(params?.n)) : code;`,
  "@/shared/lib/messages":
    "export const msg = (key, params) => `msg:${key}${params ? ' ' + JSON.stringify(params) : ''}`; export const formatMsg = (key) => `msg:${key}`;",
  "@/shared/lib/report-error": "export const reportHandledError = () => {};",
  "@/shared/lib/account-missing": `export const ACCOUNT_MISSING_CODE = "auth.account_missing";
  export const announceAccountMissing = () => {}; export const isAccountMissingResponse = () => false;`,
  "@/shared/lib/runtime-env": `export const getRuntimeEnv = () => ({ apiUrl: "http://api.test" });`,
  "@/shared/lib/runtime-locale": `export const getActiveLocale = () => "en";`,
  "@/shared/lib/sse": "export const readServerSentEvents = async () => {};",
  "@/shared/lib/oauth-return": "export const forgetOAuthReturn = () => {};",
};

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/lib/api") {
      return { shortCircuit: true, url: new URL("./api.ts", import.meta.url).href };
    }
    const source = stubs[specifier];
    if (source !== undefined) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(source)}` };
    }
    return nextResolve(specifier, context);
  },
});

const api = (await import("@/shared/lib/api")) as unknown as {
  ApiError: new (
    message: string,
    opts: { status: number; code?: string; params?: Record<string, unknown> },
  ) => Error;
  deleteContinuous: (planId: string) => Promise<void>;
  revokeApiToken: () => Promise<void>;
  removeDesktopLink: (linkId: string) => Promise<void>;
};
const { ApiError } = api;
const { knownErrorCodeMessage, userErrorMessage } = await import("./user-error.ts");

const originalConsoleError = console.error;
let logged: unknown[] = [];

beforeEach(() => {
  logged = [];
  console.error = (...args: unknown[]) => {
    logged.push(...args);
  };
});

const originalFetch = globalThis.fetch;

afterEach(() => {
  console.error = originalConsoleError;
  globalThis.fetch = originalFetch;
});

async function caught(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (err) {
    return err;
  }
  throw new Error("expected the call to fail");
}

test("a known backend code shows its catalog copy with params", () => {
  const err = new ApiError("raw server text", {
    status: 409,
    code: "share.limit",
    params: { n: 5 },
  });
  assert.equal(userErrorMessage(err, "fallback"), "Limit is 5.");
});

test("a detail that is a catalog key shows its catalog copy", () => {
  const err = new ApiError("billing.insufficient_funds", { status: 400 });
  assert.equal(userErrorMessage(err, "fallback"), "Add funds to continue.");
});

test("statuses with their own copy win over the fallback", () => {
  const cases: Array<[number, string]> = [
    [401, "msg:errors.session_expired"],
    [403, "msg:errors.forbidden"],
    [404, "msg:errors.not_found"],
    [413, "msg:errors.too_large"],
    [429, "msg:errors.rate_limit"],
    [500, "msg:errors.server"],
    [503, "msg:errors.server"],
  ];
  for (const [status, expected] of cases) {
    const err = new ApiError("Traceback (most recent call last)", { status });
    assert.equal(userErrorMessage(err, "fallback"), expected, `status ${status}`);
  }
});

test("an unknown code or validation error uses the call-site fallback, never the raw text", () => {
  const unknownCode = new ApiError("internal: KeyError 'x'", { status: 400, code: "nope.unknown" });
  assert.equal(userErrorMessage(unknownCode, "Couldn't save"), "Couldn't save");
  const conflict = new ApiError("duplicate key value violates unique constraint", { status: 409 });
  assert.equal(userErrorMessage(conflict, "Couldn't save"), "Couldn't save");
});

test("a failed fetch shows the network copy", () => {
  assert.equal(userErrorMessage(new TypeError("Failed to fetch"), "x"), "msg:errors.network");
  assert.equal(userErrorMessage(new TypeError("Load failed"), "x"), "msg:errors.network");
});

test("a request() helper that cannot reach the server shows the network copy", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("Failed to fetch");
  };
  const err = await caught(api.removeDesktopLink("link-1"));
  assert.equal(userErrorMessage(err, "Remove failed"), "msg:errors.network");
});

test("a bare 404 or 405 from a server without the route shows the version-mismatch copy", async () => {
  const cases: Array<[number, string]> = [
    [404, "Not Found"],
    [405, "Method Not Allowed"],
  ];
  for (const [status, detail] of cases) {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail }), { status });
    const err = await caught(api.removeDesktopLink("link-1"));
    assert.equal(
      userErrorMessage(err, "Remove failed"),
      'msg:shared.api.route_missing {"method":"DELETE","path":"/settings/desktop-links/link-1"}',
      `status ${status}`,
    );
  }
});

test("a 404 with a real detail still shows the not-found copy", async () => {
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: "Link not found" }), { status: 404 });
  const err = await caught(api.removeDesktopLink("link-1"));
  assert.equal(userErrorMessage(err, "Remove failed"), "msg:errors.not_found");
});

test("a wrapped error whose cause is not a network failure uses the fallback", () => {
  const err = new Error("wrapped", { cause: new TypeError("x is undefined") });
  assert.equal(userErrorMessage(err, "fallback"), "fallback");
});

test("helpers that read the response themselves keep the backend code", async () => {
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ code: "continuous.not_found", detail: "raw" }), { status: 404 });
  const removed = await caught(api.deleteContinuous("plan-1"));
  assert.equal(userErrorMessage(removed, "fallback"), "That continuous plan was not found.");

  globalThis.fetch = async () =>
    new Response(JSON.stringify({ code: "share.limit", params: { n: 2 } }), { status: 409 });
  const revoked = await caught(api.revokeApiToken());
  assert.equal(userErrorMessage(revoked, "fallback"), "Limit is 2.");
});

test("without a fallback an unknown error shows the generic copy", () => {
  assert.equal(userErrorMessage(new Error("boom")), "msg:errors.generic");
  assert.equal(userErrorMessage("a string"), "msg:errors.generic");
  assert.equal(userErrorMessage(new TypeError("x is undefined")), "msg:errors.generic");
});

test("the raw error goes to the console", () => {
  const err = new Error("raw detail");
  userErrorMessage(err, "fallback");
  assert.deepEqual(logged, [err]);
});

test("knownErrorCodeMessage returns undefined for unknown or malformed codes", () => {
  assert.equal(knownErrorCodeMessage("billing.insufficient_funds"), "Add funds to continue.");
  assert.equal(knownErrorCodeMessage("nope.unknown"), undefined);
  assert.equal(knownErrorCodeMessage("Some sentence."), undefined);
  assert.equal(knownErrorCodeMessage(null), undefined);
});
