import { test } from "node:test";
import assert from "node:assert/strict";
import type { BlackboxRunResult, OptimizationPayloadResponse } from "@/shared/types/api";
import { describeBlackboxArtifact } from "./blackbox-artifact.ts";

function payload(body: Record<string, unknown>): OptimizationPayloadResponse {
  return { optimization_id: "job", optimization_type: "blackbox", payload: body };
}

function result(best: BlackboxRunResult["best_candidate"]): BlackboxRunResult {
  return { best_candidate: best } as BlackboxRunResult;
}

test("a single-file starting point names the file", () => {
  assert.deepEqual(
    describeBlackboxArtifact(
      null,
      payload({ seed_candidate: { "AGENTS.md": "# Rules" } }),
    ),
    { kind: "file", name: "AGENTS.md" },
  );
});

test("a text starting point hands its text over for kind detection", () => {
  assert.deepEqual(
    describeBlackboxArtifact(null, payload({ seed_candidate: "import os" })),
    {
      kind: "text",
      text: "import os",
    },
  );
});

test("the result's candidate stands in while the payload is still loading", () => {
  assert.deepEqual(describeBlackboxArtifact(result("<svg/>"), null), {
    kind: "text",
    text: "<svg/>",
  });
});

test("several named parts and no candidate at all fall back to a bare version", () => {
  assert.deepEqual(
    describeBlackboxArtifact(null, payload({ seed_candidate: { system: "a", user: "b" } })),
    {
      kind: "version",
    },
  );
  assert.deepEqual(describeBlackboxArtifact(null, null), { kind: "version" });
});
