import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

import type { ContractCheckResponse } from "@/shared/types/unified-run";

const srcUrl = new URL("../../../", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    }
    return nextResolve(specifier, context);
  },
});

const { automaticTurnBody } = await import("../../../shared/ui/agent/automatic-turn.ts");
const { setupCheckFeedback } = await import("./setup-check-feedback.ts");

const CHECK = {
  ok: false,
  optimizer_resolved: "auto",
  fingerprint: "f",
  samples: [],
  checks: [
    { key: "imports", status: "passed", message: "ok" },
    { key: "metric_runs", status: "failed", message: "metric failed", detail: "KeyError: 'x'" },
    { key: "run_runs", status: "failed", message: "run failed" },
  ],
} as unknown as ContractCheckResponse;

test("the feedback lists only the failed checks, with their errors", () => {
  const body = automaticTurnBody(setupCheckFeedback(CHECK, (item) => item.message));
  assert.equal(body, "- metric failed\n\n```\nKeyError: 'x'\n```\n- run failed");
});
