import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { afterEach, beforeEach, test } from "node:test";

// The real modules pull in the generated catalogs and the fetch layer; the
// helper only needs the error class, a code lookup and a key lookup.
const stubs: Record<string, string> = {
  "@/shared/lib/api": `export class ApiError extends Error {
    constructor(message, opts) { super(message); this.status = opts.status; this.code = opts.code; this.params = opts.params; }
  }`,
  "@/shared/lib/i18n": `const known = { "billing.insufficient_funds": "Add funds to continue.", "share.limit": "Limit is {n}." };
  export const tI18n = (code, params) => known[code] ? known[code].replace("{n}", String(params?.n)) : code;`,
  "@/shared/lib/messages": "export const msg = (key) => `msg:${key}`;",
};

registerHooks({
  resolve(specifier, context, nextResolve) {
    const source = stubs[specifier];
    if (source !== undefined) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(source)}` };
    }
    return nextResolve(specifier, context);
  },
});

const { ApiError } = (await import("@/shared/lib/api")) as unknown as {
  ApiError: new (
    message: string,
    opts: { status: number; code?: string; params?: Record<string, unknown> },
  ) => Error;
};
const { knownErrorCodeMessage, userErrorMessage } = await import("./user-error.ts");

const originalConsoleError = console.error;
let logged: unknown[] = [];

beforeEach(() => {
  logged = [];
  console.error = (...args: unknown[]) => {
    logged.push(...args);
  };
});

afterEach(() => {
  console.error = originalConsoleError;
});

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
