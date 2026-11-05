import assert from "node:assert/strict";
import { test } from "node:test";

const {
  agentModels,
  datasetProfile,
  discoveredModels,
  engineCatalog,
  libraryDatasets,
  optimizerCatalog,
  resultErrorText,
  resultObject,
  sampleDatasets,
  stagedSample,
  validation,
} = await import("./setup-tool-results.ts");

test("a JSON string result parses; other strings and arrays do not", () => {
  assert.deepEqual(resultObject('{"a":1}'), { a: 1 });
  assert.equal(resultObject("User declined"), null);
  assert.equal(resultObject([1]), null);
});

test("an error result gives its plain text or its body's detail", () => {
  assert.equal(resultErrorText(" boom "), "boom");
  assert.equal(resultErrorText({ detail: "Not found" }), "Not found");
  assert.equal(resultErrorText('{"error":"bad"}'), "bad");
  assert.equal(resultErrorText({ ok: true }), "");
});

test("the optimizer catalog keeps each option and the shared contract", () => {
  const view = optimizerCatalog(
    JSON.stringify({
      artifact_kind: "repo",
      data_mode: "single",
      run_entry: "def run(artifact, input)",
      metric_signature: "def score(input, output, expected=None)",
      default: "auto",
      options: [
        { id: "gepa", available: true, unavailable_reason: null, needs_reflection_model: true },
        {
          id: "meta_harness",
          available: false,
          unavailable_reason: "No sandbox",
          needs_reflection_model: false,
        },
        { available: true },
      ],
      sandbox_available: false,
      sandbox_reason: "Sandbox off",
    }),
  );
  assert.deepEqual(view, {
    defaultId: "auto",
    options: [
      { id: "gepa", available: true, unavailableReason: "", needsReflection: true },
      {
        id: "meta_harness",
        available: false,
        unavailableReason: "No sandbox",
        needsReflection: false,
      },
    ],
    runEntry: "def run(artifact, input)",
    metricSignature: "def score(input, output, expected=None)",
    sandboxReason: "Sandbox off",
  });
  assert.equal(optimizerCatalog({ artifact_kind: "repo" }), null);
});

test("the engine catalog falls back to the id when a label is missing", () => {
  assert.deepEqual(
    engineCatalog({ engines: [{ id: "best_of_n", available: false, unavailable_reason: "Off" }] }),
    [
      {
        id: "best_of_n",
        label: "best_of_n",
        description: "",
        available: false,
        unavailableReason: "Off",
      },
    ],
  );
  assert.equal(engineCatalog("Error"), null);
});

test("the model lists keep names and the discovered total", () => {
  assert.deepEqual(agentModels({ models: [{ name: "openai/gpt-5" }, { provider: "x" }] }), [
    "openai/gpt-5",
  ]);
  assert.deepEqual(
    discoveredModels({ models: ["a", "b"], base_url: "http://h", total: 40, truncated: true }),
    { models: ["a", "b"], total: 40, baseUrl: "http://h", error: "" },
  );
  assert.deepEqual(discoveredModels({ models: [], base_url: "http://h", error: "refused" }), {
    models: [],
    total: 0,
    baseUrl: "http://h",
    error: "refused",
  });
});

test("a validation result keeps its verdict, reasons and signature fields", () => {
  assert.deepEqual(
    validation({
      valid: false,
      errors: ["Missing field"],
      warnings: [],
      signature_fields: { inputs: ["question"], outputs: [] },
    }),
    { valid: false, errors: ["Missing field"], warnings: [], fields: [["inputs", ["question"]]] },
  );
  assert.equal(validation({ errors: [] }), null);
});

test("dataset lists keep names, counts and the shared mark", () => {
  assert.deepEqual(
    libraryDatasets({
      datasets: [
        { id: "d1", name: "Mine", row_count: 10, column_count: 2, role: "owner" },
        { id: "d2", name: "Theirs", role: "viewer" },
      ],
      usage: {},
    }),
    [
      { id: "d1", name: "Mine", rows: 10, columns: 2, shared: false },
      { id: "d2", name: "Theirs", rows: null, columns: null, shared: true },
    ],
  );
  assert.deepEqual(
    sampleDatasets({
      samples: [
        {
          sample_id: "s",
          name: "Sentiment",
          description: "d",
          row_count: 5,
          columns: ["text", "label"],
        },
      ],
    }),
    [{ id: "s", name: "Sentiment", description: "d", rows: 5, columns: ["text", "label"] }],
  );
  assert.deepEqual(
    stagedSample({
      sample_id: "s",
      name: "Sentiment",
      description: "",
      row_count: 5,
      dataset_filename: "sentiment.csv",
      wizard_state: { dataset_columns: ["text", "label"] },
    }),
    {
      name: "Sentiment",
      description: "",
      rows: 5,
      filename: "sentiment.csv",
      columns: ["text", "label"],
    },
  );
});

test("a profile keeps the columns with their kinds and the split counts", () => {
  assert.deepEqual(
    datasetProfile({
      profile: {
        row_count: 100,
        column_count: 2,
        duplicate_count: 3,
        inputs: [{ name: "text", kind: "text" }],
        targets: [{ name: "label", kind: "categorical", unique_values: 2 }],
      },
      plan: { counts: { train: 70, val: 15, test: 15 } },
    }),
    {
      rows: 100,
      columns: 2,
      duplicates: 3,
      inputs: [{ name: "text", kind: "text" }],
      outputs: [{ name: "label", kind: "categorical" }],
      split: { train: 70, val: 15, test: 15 },
    },
  );
  assert.equal(datasetProfile({ plan: {} }), null);
});
