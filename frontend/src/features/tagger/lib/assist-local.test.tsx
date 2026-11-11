/*
 * node --test cannot resolve the `@/` alias or the JSX the feature barrels
 * pull in, so this runs through jiti (assist-local.runner.test.ts spawns it
 * inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { ApiError } = await import("@/shared/lib/api");
const { LOCAL_MODEL_REFUSED_CODE } = await import(
  "@/features/local-runtime/lib/subscription-model"
);
const { streamPredictions } = await import("./assist-stream");
const { assistFailureKind, assistModelPatch, rememberInterviewPick } = await import("./assist");
const { composerSeed, rememberComposerPick } = await import("@/features/settings");

interface Sent {
  url: string;
  init: RequestInit;
}

function stubFetch(response: () => Response): Sent[] {
  const sent: Sent[] = [];
  globalThis.fetch = (async (url: string | URL, init?: RequestInit) => {
    sent.push({ url: String(url), init: init ?? {} });
    return response();
  }) as typeof fetch;
  return sent;
}

// The body the local backend sends when it refuses a managed tagging model
// (backend/core/service_gateway/tagging.py, `_refuse_local_managed`).
const refusal = () =>
  new Response(
    JSON.stringify({
      code: LOCAL_MODEL_REFUSED_CODE,
      params: {},
      detail: "Managed models are not available in the Mac app.",
    }),
    { status: 422, headers: { "Content-Type": "application/json" } },
  );

void describe("local tagging requests", () => {
  void it("posts the round's rows and reports a local refusal as local_key", async () => {
    const sent = stubFetch(refusal);
    const errors: Array<string | undefined> = [];
    await streamPredictions("session-1", ["row-1", "row-2"], {
      onPrediction: () => assert.fail("no prediction expected"),
      onDone: () => assert.fail("no completion expected"),
      onError: (code) => errors.push(code),
    });

    assert.equal(sent.length, 1);
    const [request] = sent as [Sent];
    assert.match(request.url, /\/tagging-sessions\/session-1\/assist\/predict\/stream$/);
    assert.equal(request.init.method, "POST");
    assert.deepEqual(JSON.parse(String(request.init.body)), { row_ids: ["row-1", "row-2"] });
    assert.deepEqual(errors, [LOCAL_MODEL_REFUSED_CODE]);
    assert.equal(assistFailureKind(errors[0], "predict"), "local_key");
  });

  void it("keeps the generic error for other failures", async () => {
    stubFetch(() => new Response("upstream down", { status: 502 }));
    const errors: Array<string | undefined> = [];
    await streamPredictions("session-1", ["row-1"], {
      onPrediction: () => {},
      onDone: () => {},
      onError: (code) => errors.push(code),
    });
    assert.deepEqual(errors, [undefined]);
    assert.equal(assistFailureKind(errors[0], "predict"), "predict");
    assert.equal(assistFailureKind(new Error("boom"), "autotag"), "autotag");
    assert.equal(
      assistFailureKind(
        new ApiError("x", { status: 400, code: "billing.byok_missing_connection" }),
        "synthesize",
      ),
      "synthesize",
    );
  });

  void it("maps a refused synthesize or autotag call to local_key", () => {
    const err = new ApiError("refused", { status: 422, code: LOCAL_MODEL_REFUSED_CODE });
    assert.equal(assistFailureKind(err, "synthesize"), "local_key");
    assert.equal(assistFailureKind(err, "autotag"), "local_key");
  });

  void it("keeps a BYOK tagging model's source and provider on the session", () => {
    assert.deepEqual(
      assistModelPatch({
        name: " openai/gpt-4o-mini ",
        token_source: "byok",
        byok_provider: "openai",
      }),
      {
        model: "openai/gpt-4o-mini",
        modelParams: { token_source: "byok", byok_provider: "openai" },
      },
    );
  });

  void it("an interview with a cleared pick keeps the agent panel's saved model", () => {
    const store = new Map<string, string>();
    const g = globalThis as { window?: unknown };
    g.window = {
      __APP_ENV__: { localMode: true },
      localStorage: {
        getItem: (k: string) => store.get(k) ?? null,
        setItem: (k: string, v: string) => store.set(k, v),
        removeItem: (k: string) => store.delete(k),
      },
    };
    try {
      rememberComposerPick(true, "anthropic/claude-sonnet-5", "high");
      rememberInterviewPick({ interviewModel: null, interviewEffort: null });
      assert.deepEqual(composerSeed(true), { model: "anthropic/claude-sonnet-5", effort: "high" });
      rememberInterviewPick({ interviewModel: "openrouter/openai/gpt-5.5", interviewEffort: "low" });
      assert.deepEqual(composerSeed(true), { model: "openrouter/openai/gpt-5.5", effort: "low" });
    } finally {
      delete g.window;
    }
  });
});
