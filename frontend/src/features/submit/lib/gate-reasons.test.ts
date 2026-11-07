import assert from "node:assert/strict";
import { test } from "node:test";

import { gateReasons } from "./gate-reasons.ts";

const CATALOG: Record<string, string> = {
  "repo_gate.unknown_connector":
    "The service {service} is not a connector. Use one of these names: {known}.",
  "repo_gate.unenforced_cloud": "optimize.toml sets {key}. Remove it to run.",
};

const translate = (code: string, params?: Record<string, unknown>) =>
  CATALOG[code]?.replace(/\{(\w+)\}/g, (_, name: string) => String(params?.[name]));

const refused = (failures: unknown) => ({
  status: 409,
  code: "submission.validation_failed",
  params: { failures },
});

test("each gate failure is worded from its catalog key and parameters", () => {
  const reasons = gateReasons(
    refused([
      { key: "repo_gate.unknown_connector", params: { service: "jira", known: "github, linear" } },
      { key: "repo_gate.unenforced_cloud", params: { key: "proposer.network" } },
    ]),
    translate,
  );
  assert.deepEqual(reasons, [
    "The service jira is not a connector. Use one of these names: github, linear.",
    "optimize.toml sets proposer.network. Remove it to run.",
  ]);
});

test("a failure with no catalog copy or a bad shape is left out", () => {
  const reasons = gateReasons(
    refused([
      { key: "repo_gate.not_in_catalog", params: {} },
      { params: {} },
      null,
      "repo_gate.unknown_connector",
      { key: "repo_gate.unenforced_cloud", params: { key: "metric.files" } },
    ]),
    translate,
  );
  assert.deepEqual(reasons, ["optimize.toml sets metric.files. Remove it to run."]);
});

test("other errors and a refusal from an older server give no reasons", () => {
  assert.deepEqual(gateReasons(new Error("boom"), translate), []);
  assert.deepEqual(gateReasons(null, translate), []);
  assert.deepEqual(
    gateReasons({ code: "submission.validation_failed", params: {} }, translate),
    [],
  );
  assert.deepEqual(
    gateReasons(
      {
        code: "budget.pending",
        params: { failures: [{ key: "repo_gate.unenforced_cloud", params: {} }] },
      },
      translate,
    ),
    [],
  );
});
