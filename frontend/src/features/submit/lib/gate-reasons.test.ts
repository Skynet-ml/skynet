import assert from "node:assert/strict";
import { test } from "node:test";

import { applySecretHostIssues, gateReasons, secretHostIssues } from "./gate-reasons.ts";

const CATALOG: Record<string, string> = {
  "repo_gate.unknown_connector":
    "The service {service} is not a connector. Use one of these names: {known}.",
  "repo_gate.unenforced_cloud": "optimize.toml sets {key}. Remove it to run.",
  "repo_gate.secret_host_unlisted": "{secret} has no allowed hosts but goes to {host}.",
  "repo_gate.secret_host_not_allowed": "{secret} may go only to {allowed} but goes to {host}.",
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

test("a secret refused for a host gets its fix message under its own name", () => {
  const issues = secretHostIssues(
    refused([
      {
        key: "repo_gate.secret_host_unlisted",
        params: { secret: "API_KEY", host: "a.example.com" },
      },
      {
        key: "repo_gate.secret_host_not_allowed",
        params: { secret: "API_KEY", allowed: "b.example.com", host: "c.example.com" },
      },
      { key: "repo_gate.secret_host_unlisted", params: { secret: "TOKEN", host: "d.example.com" } },
      { key: "repo_gate.unenforced_cloud", params: { key: "proposer.network", secret: "TOKEN" } },
      { key: "repo_gate.secret_host_unlisted", params: { host: "e.example.com" } },
    ]),
    translate,
  );
  assert.deepEqual({ ...issues }, {
    API_KEY:
      "API_KEY has no allowed hosts but goes to a.example.com. API_KEY may go only to b.example.com but goes to c.example.com.",
    TOKEN: "TOKEN has no allowed hosts but goes to d.example.com.",
  });
  assert.deepEqual({ ...secretHostIssues(new Error("boom"), translate) }, {});
});

test("a secret named like an object property gets only its own fix message", () => {
  const issues = secretHostIssues(
    refused([
      {
        key: "repo_gate.secret_host_unlisted",
        params: { secret: "__proto__", host: "a.example.com" },
      },
      {
        key: "repo_gate.secret_host_unlisted",
        params: { secret: "toString", host: "b.example.com" },
      },
    ]),
    translate,
  );
  assert.equal(issues["__proto__"], "__proto__ has no allowed hosts but goes to a.example.com.");
  assert.equal(issues["toString"], "toString has no allowed hosts but goes to b.example.com.");
  assert.deepEqual(Object.keys(issues).sort(), ["__proto__", "toString"]);

  const rows = applySecretHostIssues(
    [{ name: "__proto__" }, { name: "toString" }, { name: "constructor" }],
    issues,
  );
  assert.deepEqual(
    rows.map((row) => row.hostIssue),
    [
      "__proto__ has no allowed hosts but goes to a.example.com.",
      "toString has no allowed hosts but goes to b.example.com.",
      undefined,
    ],
  );
  const none = applySecretHostIssues(
    [{ name: "toString" }, { name: "constructor" }],
    secretHostIssues(new Error("boom"), translate),
  );
  assert.deepEqual(
    none.map((row) => row.hostIssue),
    [undefined, undefined],
  );
});

test("each refused submit sets or clears every row, so a fixed row loses its old message", () => {
  const failA = secretHostIssues(
    refused([
      { key: "repo_gate.secret_host_unlisted", params: { secret: "A", host: "a.example.com" } },
    ]),
    translate,
  );
  let rows = applySecretHostIssues(
    [
      { name: "A", allowedHosts: "" },
      { name: "B", allowedHosts: "" },
    ],
    failA,
  );
  assert.deepEqual(
    rows.map((row) => row.hostIssue),
    ["A has no allowed hosts but goes to a.example.com.", undefined],
  );

  rows = rows.map((row) => (row.name === "A" ? { ...row, allowedHosts: "a.example.com" } : row));
  const failB = secretHostIssues(
    refused([
      { key: "repo_gate.secret_host_unlisted", params: { secret: "B", host: "b.example.com" } },
    ]),
    translate,
  );
  rows = applySecretHostIssues(rows, failB);
  assert.deepEqual(
    rows.map((row) => row.hostIssue),
    [undefined, "B has no allowed hosts but goes to b.example.com."],
  );

  rows = applySecretHostIssues(rows, secretHostIssues(new Error("network"), translate));
  assert.deepEqual(
    rows.map((row) => row.hostIssue),
    [undefined, undefined],
  );
});
