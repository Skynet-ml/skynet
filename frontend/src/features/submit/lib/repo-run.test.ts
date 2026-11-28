import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const srcUrl = new URL("../../../", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    }
    if (specifier.startsWith(".") && !specifier.endsWith(".ts")) {
      return { shortCircuit: true, url: new URL(`${specifier}.ts`, context.parentURL).href };
    }
    return nextResolve(specifier, context);
  },
});

const {
  initialWizardV2State,
  emptyRepoDraft,
  envReplaceNames,
  fileProposerCommand,
  parseAllowedHosts,
  repoIssue,
  taskIssue,
  undecidedEnvClashes,
  withLocalFolder,
} = await import("./wizard-v2.ts");
const { stepPages } = await import("./wizard-pages.ts");
const { computeStepIssue } = await import("./step-issue.ts");
const {
  LITELLM_TO_BYOK_PROVIDER,
  contractMissing,
  contractText,
  finishedHardeningJobId,
  metricCommandText,
  repoBaseText,
  RUN_SECRETS_MAX,
  TooManyRunSecretsError,
  UndecidedEnvClashesError,
  envSecretClashesState,
  envSubmitFailure,
  repoRunRequest,
  withEnvSecrets,
  withLocalEnvImport,
} = await import("./repo-run.ts");
const { BYOK_PROVIDERS, litellmProviderForByok } = await import("../../billing/lib/byok.ts");
const { prepareModelConfig } = await import("./model-config.ts");
const { HARNESS_DEFAULT_PICK, harnessPickerValue, pickHarness, proposerHeader } = await import("./engine-contract.ts");

const model = { name: "openai/gpt-default", token_source: "managed" as const };

function repoState() {
  const state = initialWizardV2State();
  state.task.objective = "Make the build faster.";
  state.task.repo = {
    ...emptyRepoDraft(),
    repository: "example/site",
    commit: "a".repeat(40),
    editablePaths: ["src", " "],
    setupCommand: "npm ci",
  };
  state.inputs.mode = "single";
  state.optimizer.choice = "gepa";
  state.optimizer.reflectionModel = model;
  state.optimizer.taskModel = model;
  state.workspace.metricCode = "def score(repo_path, case=None):\n    return 1.0";
  return state;
}

const options = { username: "u", tokenSource: "managed" as const, maxCostCents: 500 };

test("a repository run submits its repo, commit, paths and setup command", () => {
  const request = repoRunRequest(repoState(), options);
  assert.ok(request);
  assert.deepEqual(request.target, {
    kind: "repo",
    repo: {
      provider: "github",
      repository: "example/site",
      branch: null,
      commit: "a".repeat(40),
      editable_paths: ["src"],
    },
    setup_command: "npm ci",
  });
  assert.deepEqual(request.strategy, { mode: "single", engine: "gepa" });
  assert.equal(request.scorer.kind, "python");
  assert.equal(request.max_cost_cents, 500);
  assert.equal(request.task_model_config, null);
});

test("a repository run never sends the retired stop score, also from a clone or a restored setup", () => {
  for (const stop of [0.9, null]) {
    const state = repoState();
    state.optimizer.budget = { max_scorer_runs: 8, max_iterations: 4, stop_at_score: stop } as typeof state.optimizer.budget;
    const request = repoRunRequest(state, options);
    assert.ok(request);
    assert.deepEqual(request.budget, { max_scorer_runs: 8, max_iterations: 4 });
  }
  const fresh = repoRunRequest(repoState(), options);
  assert.ok(fresh);
  assert.equal("stop_at_score" in fresh.budget, false);
});

test("a repository run sends the chosen sandbox provider and omits it when none is chosen", () => {
  const unset = repoRunRequest(repoState(), options);
  assert.ok(unset);
  assert.equal("sandbox_provider" in unset, false);

  const state = repoState();
  state.optimizer.sandboxProvider = "vercel";
  assert.equal(repoRunRequest(state, options)?.sandbox_provider, "vercel");

  // A draft saved before the picker existed has no field at all.
  const older = repoState();
  delete older.optimizer.sandboxProvider;
  assert.equal("sandbox_provider" in repoRunRequest(older, options)!, false);
});

test("a repository run sends no harness until the user picks one (SKY-753)", () => {
  const fresh = repoRunRequest(repoState(), options);
  assert.ok(fresh?.proposer);
  assert.equal("harness" in fresh.proposer, false);
  assert.equal(fresh.proposer.max_tool_calls, 100);

  const state = repoState();
  state.optimizer.proposer = { ...state.optimizer.proposer, harness: "pi" };
  assert.equal(repoRunRequest(state, options)?.proposer?.harness, "pi");
});

test("the contract file's proposer sends no agent; a picked agent is sent (SKY-771)", () => {
  const state = repoState();
  assert.notEqual(fileProposerCommand(state.optimizer.proposer, ["bash", "propose.sh"], "autoresearch"), null);
  const fromFile = repoRunRequest(state, options)!.proposer!;
  assert.equal("harness" in fromFile, false);
  assert.equal("run_command" in fromFile, false);
  assert.equal("install_command" in fromFile, false);

  state.optimizer.proposer = pickHarness(state.optimizer.proposer, "pi");
  assert.equal(fileProposerCommand(state.optimizer.proposer, ["bash", "propose.sh"], "autoresearch"), null);
  assert.equal(repoRunRequest(state, options)?.proposer?.harness, "pi");
});

test("the harness picker starts empty, so picking Codex sends Codex (SKY-753)", () => {
  const state = repoState();
  // Radix fires onValueChange only for a new value, so the unset picker must not hold "codex".
  assert.equal(harnessPickerValue(state.optimizer.proposer), "");

  state.optimizer.proposer = { ...state.optimizer.proposer, harness: "codex" };
  assert.equal(harnessPickerValue(state.optimizer.proposer), "codex");
  assert.equal(repoRunRequest(state, options)?.proposer?.harness, "codex");
});

test("the default harness item drops the picked harness and host CLI again (SKY-753)", () => {
  const state = repoState();
  state.optimizer.proposer = pickHarness({ ...state.optimizer.proposer, host_cli: "claude" }, "pi");
  assert.equal(state.optimizer.proposer.harness, "pi");

  state.optimizer.proposer = pickHarness(state.optimizer.proposer, HARNESS_DEFAULT_PICK);
  assert.equal("harness" in state.optimizer.proposer, false);
  assert.equal("host_cli" in state.optimizer.proposer, false);
  assert.equal(state.optimizer.proposer.max_tool_calls, 100);
  assert.equal(harnessPickerValue(state.optimizer.proposer), "");
  // With no harness, the repository's [proposer] entry runs again.
  assert.equal("harness" in repoRunRequest(state, options)!.proposer!, false);
});

test("the closed proposer header shows the server default until the user picks a harness (SKY-753)", () => {
  const state = repoState();
  // The request sends no harness yet, so the header must not claim Codex.
  assert.deepEqual(proposerHeader(state.optimizer.proposer), {
    labelKey: "submit.v2.optimizer.proposer.harness_default",
  });

  state.optimizer.proposer = { ...state.optimizer.proposer, harness: "pi" };
  assert.deepEqual(proposerHeader(state.optimizer.proposer), { harness: "pi" });
});

test("a repository Skynet created is flagged in the run request", () => {
  const state = repoState();
  state.task.repo = { ...state.task.repo!, createdBySkynet: true };
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.equal(request.target.repo?.created_by_skynet, true);
  assert.equal(repoRunRequest(repoState(), options)?.target.repo?.created_by_skynet, undefined);
});

test("a GitLab subgroup path is valid and submits with its host", () => {
  const state = repoState();
  state.task.repo = { ...state.task.repo!, provider: "gitlab", repository: "team/sub/site" };
  assert.equal(repoIssue(state.task.repo), null);
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.equal(request.target.repo?.provider, "gitlab");
  assert.equal(request.target.repo?.repository, "team/sub/site");
});

test("a subgroup path is not a GitHub or Bitbucket repository", () => {
  const repo = { ...repoState().task.repo!, repository: "team/sub/site" };
  assert.notEqual(repoIssue(repo), null);
  assert.notEqual(repoIssue({ ...repo, provider: "bitbucket" }), null);
});

test("Auto on a repository sends the auto strategy", () => {
  const state = repoState();
  state.optimizer.choice = "auto";
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.deepEqual(request.strategy, { mode: "auto" });
  assert.equal(request.shinka, null);
  assert.deepEqual(request.extra_reflection_model_configs, []);
  assert.equal(computeStepIssue(state, "optimizer", null), null);
});

test("a repository run waits for a reflection model and a scorer", () => {
  const noModel = repoState();
  noModel.optimizer.reflectionModel = null;
  assert.equal(repoRunRequest(noModel, options), null);
  const noScorer = repoState();
  noScorer.workspace.metricCode = "  ";
  assert.equal(repoRunRequest(noScorer, options), null);
});

test("a repository clone drops a stored scorer model the code does not name", () => {
  const state = repoState();
  state.workspace.metricModel = { name: "anthropic/legacy", token_source: "byok" };
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
  state.workspace.metricCode = "def score(repo_path, case=None):\n    return llm('x')";
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
});

test("a repository clone drops a stored scorer model the code replaces", () => {
  const state = repoState();
  state.workspace.metricModel = { name: "anthropic/legacy", token_source: "byok" };
  state.workspace.metricCode = "llm('x', model='anthropic/judge')";
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
  state.workspace.metricCode = "llm('x', model='anthropic/legacy')";
  assert.deepEqual(
    repoRunRequest(state, options)?.scorer.model,
    prepareModelConfig(state.workspace.metricModel),
  );
});

test("a new repository scorer leaves its declared model to the backend", () => {
  const state = repoState();
  state.workspace.metricCode = "llm(model='anthropic/judge')";
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
});

test("a new repository scorer leaves reflection fallback to the backend", () => {
  const state = repoState();
  state.workspace.metricCode = "llm('x')";
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
});

test("a repository run leaves ambiguous declarations for backend validation", () => {
  const state = repoState();
  state.workspace.metricCode = "llm('x'); llm('y', model='anthropic/judge')";
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
});

test("a repository run skips inputs and needs only the scorer", () => {
  const state = repoState();
  state.inputs.mode = null;
  assert.equal(computeStepIssue(state, "inputs", null), null);
  assert.equal(computeStepIssue(state, "metric", null), null);
  state.workspace.metricCode = " ";
  assert.equal(computeStepIssue(state, "metric", null), "submit.v2.metric.issue.metric_code");
  assert.deepEqual(stepPages("metric", state), ["metric", "harden", "overview"]);
});

test("the task step names what a repository still lacks", () => {
  const state = repoState();
  assert.equal(computeStepIssue(state, "task", null), null);
  state.task.repo = { ...state.task.repo!, commit: "abc" };
  assert.equal(computeStepIssue(state, "task", null), "submit.v2.repo.issue.commit");
  state.task.repo = { ...emptyRepoDraft(), repository: "not a repo" };
  assert.equal(computeStepIssue(state, "task", null), "submit.v2.repo.issue.repository");
});

// A restored draft or a clone from an older build can still name Droid, which
// the backend refuses at submit.
const staleDroid = {
  name: "openai/gpt-default",
  token_source: "subscription",
  cli_provider: "droid",
} as never;

test("a model config drops a CLI the app no longer offers", () => {
  assert.equal(prepareModelConfig(staleDroid).cli_provider, null);
  const kept = { ...model, token_source: "subscription" as const, cli_provider: "codex" as const };
  assert.equal(prepareModelConfig(kept).cli_provider, "codex");
  assert.equal("cli_provider" in prepareModelConfig(model), false);
});

test("a repository run sends no stale CLI for its reflection models", () => {
  const state = repoState();
  state.optimizer.choice = "shinka_evolve";
  state.optimizer.reflectionModel = staleDroid;
  state.optimizer.extraReflectionModels = [staleDroid];
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.equal(request.reflection_model_config?.cli_provider, null);
  assert.equal(request.extra_reflection_model_configs?.[0]?.cli_provider, null);
});

test("a repository run sends economy on the models that chose it, not for the run", () => {
  const state = repoState();
  state.optimizer.choice = "shinka_evolve";
  state.optimizer.reflectionModel = { ...model, economy_mode: true };
  state.optimizer.extraReflectionModels = [model];
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.equal(request.reflection_model_config?.extra?.economy_mode, true);
  assert.equal(request.extra_reflection_model_configs?.[0]?.extra, undefined);
  assert.equal("economy_mode" in request, false);
});

test("a repository run sends its cases only when it needs data", () => {
  const state = repoState();
  state.inputs.cases = [{ input: { url: "/" } }];
  state.inputs.inputFields = ["url"];
  assert.equal(repoRunRequest(state, options)?.cases, undefined);
  state.task.needsData = true;
  assert.deepEqual(repoRunRequest(state, options)?.cases, [{ input: { url: "/" } }]);
  assert.equal(computeStepIssue(state, "inputs", null), null);
  state.inputs.cases = [];
  state.inputs.mode = null;
  assert.equal(repoRunRequest(state, options)?.cases, undefined);
  assert.notEqual(computeStepIssue(state, "inputs", null), null);
});

test("a repository run sends a staged dataset by reference", () => {
  const state = repoState();
  state.task.needsData = true;
  state.inputs.stagedDatasetId = "ds-1";
  state.inputs.inputFields = ["url"];
  state.inputs.expectedField = null;
  const request = repoRunRequest(state, options);
  assert.equal(request?.cases, undefined);
  assert.equal(request?.staged_dataset_id, "ds-1");
  assert.deepEqual(request?.input_fields, ["url"]);
  state.task.needsData = false;
  assert.equal(repoRunRequest(state, options)?.staged_dataset_id, undefined);
});

test("a repository run carries no hardening job of its own", () => {
  assert.equal("hardening_job_id" in (repoRunRequest(repoState(), options) ?? {}), false);
});

test("the run submit takes the hardening job id only from a finished job", async () => {
  const reads: string[] = [];
  const jobOf = (status: string) => async (draftId: string) => {
    reads.push(draftId);
    return { id: "job-1", status } as never;
  };
  for (const status of ["clean_stop", "budget_stop", "done"]) {
    assert.equal(await finishedHardeningJobId("local", "draft-1", jobOf(status)), "job-1", status);
  }
  for (const status of ["preflight", "preflight_failed", "running", "waiting_review", "waiting_fix", "rejected", "failed", "stale"]) {
    assert.equal(await finishedHardeningJobId("local", "draft-1", jobOf(status)), null, status);
  }
  assert.ok(reads.every((id) => id === "draft-1"));
});

test("a setup without a hardening job submits as before", async () => {
  let reads = 0;
  const read = async () => {
    reads += 1;
    return null;
  };
  assert.equal(await finishedHardeningJobId("local", null, read), null);
  assert.equal(reads, 0);
  assert.equal(await finishedHardeningJobId("local", "draft-1", read), null);
  assert.equal(reads, 1);
});

test("a failed hardening read never blocks the run submit", async () => {
  const read = async () => {
    throw new Error("offline");
  };
  assert.equal(await finishedHardeningJobId("local", "draft-1", read), null);
});

test("a hosted run sends no hardening job id", async () => {
  let reads = 0;
  const read = async () => {
    reads += 1;
    return { id: "job-1", status: "done" } as never;
  };
  assert.equal(await finishedHardeningJobId("hosted", "draft-1", read), null);
  assert.equal(reads, 0);
});

test("a run whose contract names a metric command sends no hardening job id (SKY-770)", async () => {
  let reads = 0;
  const read = async () => {
    reads += 1;
    return { id: "job-1", status: "done" } as never;
  };
  // The job hardened metric.py, which the run does not use.
  assert.equal(await finishedHardeningJobId("local", "draft-1", read, ["node", "score.js"]), null);
  assert.equal(reads, 0);
});

test("a repository run sends each secret with its allowed hosts as a list", () => {
  const state = repoState();
  state.task.repo = {
    ...state.task.repo!,
    secrets: [
      { name: " API_KEY ", value: "v1", allowedHosts: "API.example.com., *.example.com api.example.com" },
      { name: "OTHER", value: "v2", allowedHosts: "" },
    ],
  };
  assert.equal(computeStepIssue(state, "task", null), null);
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.deepEqual((request.target as { repo: { secrets?: unknown } }).repo.secrets, [
    { name: "API_KEY", value: "v1", allowed_hosts: ["api.example.com", "*.example.com"] },
    { name: "OTHER", value: "v2", allowed_hosts: [] },
  ]);
});

const SERVICES_TOML = [
  "[services.geocode]",
  'url = "https://Maps.GoogleApis.com/maps/api/geocode/json"',
  'headers = { X-Goog-Api-Key = "${MAPS_KEY}" }',
  "",
  "[services.search]",
  'url = "https://api.search.example/v1"',
  "",
  "[services.search.headers]",
  'X-Api-Key = "${SEARCH_KEY}"',
  "",
  "[metric]",
  'note = "${NOT_A_SERVICE}"',
].join("\n");

test("a repository run sends the saved secrets its services name, by id, to their host only", () => {
  const state = repoState();
  state.workspace.files = { ...state.workspace.files, "ascent.toml": SERVICES_TOML };
  state.task.repo = { ...state.task.repo!, secrets: [{ name: "SEARCH_KEY", value: "typed", allowedHosts: "api.search.example" }] };
  const savedSecrets = [
    { id: "sec-maps", name: "MAPS_KEY", allowed_hosts: ["maps.googleapis.com"] },
    { id: "sec-search", name: "SEARCH_KEY", allowed_hosts: ["api.search.example"] },
    { id: "sec-other", name: "NOT_A_SERVICE", allowed_hosts: ["maps.googleapis.com"] },
    { id: "sec-unused", name: "UNUSED_KEY", allowed_hosts: ["maps.googleapis.com"] },
  ];
  const request = repoRunRequest(state, { ...options, savedSecrets });
  assert.ok(request);
  // The typed SEARCH_KEY wins over the saved one; only service secrets go, never a value.
  assert.deepEqual((request.target as { repo: { secrets?: unknown } }).repo.secrets, [
    { name: "SEARCH_KEY", value: "typed", allowed_hosts: ["api.search.example"] },
    { name: "MAPS_KEY", saved_secret_id: "sec-maps", allowed_hosts: ["maps.googleapis.com"] },
  ]);
});

test("a saved secret goes only to a host its owner approved, never to the host an edited toml names", () => {
  const state = repoState();
  state.workspace.files = { ...state.workspace.files, "ascent.toml": SERVICES_TOML };
  state.task.repo = { ...state.task.repo!, secrets: [] };
  const savedSecrets = [
    // Approved for another host: an agent pointed the toml elsewhere.
    { id: "sec-maps", name: "MAPS_KEY", allowed_hosts: ["attacker.example.net"] },
    // Saved before SKY-641: no approved host, so it goes nowhere.
    { id: "sec-search", name: "SEARCH_KEY", allowed_hosts: [] },
  ];
  const request = repoRunRequest(state, { ...options, savedSecrets });
  assert.ok(request);
  assert.equal("secrets" in (request.target as { repo: object }).repo, false);
  // A secret with no host list at all is treated the same way.
  const legacy = repoRunRequest(state, { ...options, savedSecrets: [{ id: "sec-maps", name: "MAPS_KEY" }] });
  assert.equal("secrets" in (legacy!.target as { repo: object }).repo, false);
});

test("a saved secret approved for a *.suffix goes to a host under it with that approved entry", () => {
  const state = repoState();
  state.workspace.files = { ...state.workspace.files, "ascent.toml": SERVICES_TOML };
  state.task.repo = { ...state.task.repo!, secrets: [] };
  const savedSecrets = [
    { id: "sec-maps", name: "MAPS_KEY", allowed_hosts: ["*.googleapis.com"] },
    // "*.h.example" covers only hosts under "h.example", not "api.search.example".
    { id: "sec-search", name: "SEARCH_KEY", allowed_hosts: ["*.h.example"] },
  ];
  const request = repoRunRequest(state, { ...options, savedSecrets });
  assert.ok(request);
  assert.deepEqual((request.target as { repo: { secrets?: unknown } }).repo.secrets, [
    { name: "MAPS_KEY", saved_secret_id: "sec-maps", allowed_hosts: ["*.googleapis.com"] },
  ]);
});

test("a repository run with no saved secret for its services sends only the typed ones", () => {
  const state = repoState();
  state.workspace.files = { ...state.workspace.files, "ascent.toml": SERVICES_TOML };
  state.task.repo = { ...state.task.repo!, secrets: [] };
  const request = repoRunRequest(state, { ...options, savedSecrets: [{ id: "x", name: "UNUSED_KEY" }] });
  assert.ok(request);
  assert.equal("secrets" in (request.target as { repo: object }).repo, false);
});

test("a repository run with no secrets sends no secrets key", () => {
  const state = repoState();
  state.task.repo = { ...state.task.repo!, secrets: [] };
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.equal("secrets" in (request.target as { repo: object }).repo, false);
});

test("the task step names what a repository secret still lacks", () => {
  const state = repoState();
  const withSecrets = (secrets: { name: string; value: string; allowedHosts: string }[]) => {
    state.task.repo = { ...state.task.repo!, secrets };
    return computeStepIssue(state, "task", null);
  };
  const ok = { name: "API_KEY", value: "v", allowedHosts: "api.example.com" };
  assert.equal(withSecrets([ok]), null);
  assert.equal(withSecrets([{ ...ok, name: "1KEY" }]), "submit.v2.repo.issue.secret_name");
  assert.equal(withSecrets([{ ...ok, name: "" }]), "submit.v2.repo.issue.secret_name");
  assert.equal(withSecrets([{ ...ok, value: "" }]), "submit.v2.repo.issue.secret_value");
  assert.equal(withSecrets([{ ...ok, allowedHosts: "*.com" }]), "submit.v2.repo.issue.secret_hosts");
  assert.equal(withSecrets([{ ...ok, allowedHosts: "localhost" }]), "submit.v2.repo.issue.secret_hosts");
  assert.equal(withSecrets([{ ...ok, allowedHosts: "https://api.example.com" }]), "submit.v2.repo.issue.secret_hosts");
  const hosts = (n: number) => Array.from({ length: n }, (_, i) => `h${i}.example.com`).join(",");
  assert.equal(withSecrets([{ ...ok, allowedHosts: hosts(20) }]), null);
  assert.equal(withSecrets([{ ...ok, allowedHosts: hosts(21) }]), "submit.v2.repo.issue.secret_hosts");
  assert.equal(withSecrets([ok, { ...ok, name: " API_KEY" }]), "submit.v2.repo.issue.secret_duplicate");
});

test("a secret value may have up to 16384 characters, counted as the server counts them", () => {
  const state = repoState();
  const withValue = (value: string) => {
    state.task.repo = { ...state.task.repo!, secrets: [{ name: "API_KEY", value, allowedHosts: "" }] };
    return computeStepIssue(state, "task", null);
  };
  assert.equal(withValue("x".repeat(16384)), null);
  assert.equal(withValue("😀".repeat(16384)), null);
  assert.equal(withValue("x".repeat(16385)), "submit.v2.repo.issue.secret_value_long");
});

test("a run may have up to 100 secrets", () => {
  const state = repoState();
  const withCount = (n: number) => {
    state.task.repo = {
      ...state.task.repo!,
      secrets: Array.from({ length: n }, (_, i) => ({ name: `KEY_${i}`, value: "v", allowedHosts: "" })),
    };
    return computeStepIssue(state, "task", null);
  };
  assert.equal(withCount(100), null);
  assert.equal(withCount(101), "submit.v2.repo.issue.secret_count");
});

test("a draft with a blank secret value still gives the metric step its repository", () => {
  const state = repoState();
  // A reload or a restored draft blanks every secret value.
  state.task.repo = { ...state.task.repo!, secrets: [{ name: "API_KEY", value: "", allowedHosts: "" }] };
  assert.equal(repoIssue(state.task.repo), null);
  assert.equal(computeStepIssue(state, "task", null), "submit.v2.repo.issue.secret_value");
});

test("allowed hosts lose every trailing dot, as the server's rstrip does", () => {
  assert.deepEqual(parseAllowedHosts("api.example.com.., *.Example.COM."), ["api.example.com", "*.example.com"]);
});

test("a repository run sends the draft's repository files and omits them when there are none", () => {
  const none = repoRunRequest(repoState(), options);
  assert.ok(none);
  assert.equal("draft_files" in none.target.repo, false);

  const state = repoState();
  state.workspace.files = { "src/app.py": "x = 2\n", "ascent.toml": "version = 1\n" };
  assert.deepEqual(repoRunRequest(state, options)?.target.repo.draft_files, {
    "src/app.py": "x = 2\n",
    "ascent.toml": "version = 1\n",
  });
});

test("a repository run sends the drafts' base commit only with the drafts", () => {
  const base = "c".repeat(40);
  const state = repoState();
  state.workspace.filesBase = base;
  assert.equal("draft_base" in repoRunRequest(state, options)!.target.repo, false);

  state.workspace.files = { "src/app.py": "x = 2\n" };
  assert.equal(repoRunRequest(state, options)?.target.repo.draft_base, base);

  // Older drafts recorded no base; the run then skips the check.
  state.workspace.filesBase = undefined;
  assert.equal("draft_base" in repoRunRequest(state, options)!.target.repo, false);
});

test("a repository run never sends an empty ascent.toml draft", () => {
  const state = repoState();
  state.workspace.files = { "ascent.toml": " \n", "src/app.py": "x = 2\n" };
  assert.deepEqual(repoRunRequest(state, options)?.target.repo.draft_files, { "src/app.py": "x = 2\n" });

  state.workspace.files = { "ascent.toml": "" };
  assert.equal("draft_files" in repoRunRequest(state, options)!.target.repo, false);
});

test("the Metric screen holds only when neither the draft nor the repository has ascent.toml", () => {
  assert.equal(contractMissing("", ""), true);
  assert.equal(contractMissing(undefined, "  \n"), true);
  assert.equal(contractMissing("version = 1\n", ""), false);
  assert.equal(contractMissing("", "version = 1\n"), false);
  // A read still loading, or no pane to read it: the server decides.
  assert.equal(contractMissing("", undefined), false);
});

test("only a repository read that found no ascent.toml holds the Metric screen", () => {
  const holds = (result: { text: string } | { issue: string } | null) =>
    contractMissing("", repoBaseText("ascent.toml", result));
  assert.equal(holds({ issue: "missing" }), true);
  assert.equal(holds({ issue: "binary" }), false);
  assert.equal(holds({ issue: "too_large" }), false);
  assert.equal(holds(null), false);
  assert.equal(holds({ text: "version = 1\n" }), false);
  // Other files keep an empty base, so the Changes pane does not change.
  assert.equal(repoBaseText("src/a.py", null), "");
});

test("the contract text is the draft ascent.toml, else the repository's file (SKY-770)", () => {
  const repo = 'version = 1\n[metric]\nentry = ["node", "eval/score.js"]\n';
  assert.equal(contractText("version = 1\n", repo), "version = 1\n");
  assert.equal(contractText("  \n", repo), repo);
  assert.equal(contractText(undefined, repo), repo);
  assert.equal(contractText(undefined, undefined), "");
});

test("the metric command reads as one line, with odd words quoted (SKY-770)", () => {
  assert.equal(metricCommandText(["cargo", "run", "--bin", "score"]), "cargo run --bin score");
  assert.equal(metricCommandText(["sh", "-c", "go run ./eval"]), 'sh -c "go run ./eval"');
});

test("a run whose contract names a metric command sends no metric code (SKY-770)", () => {
  const state = repoState();
  state.workspace.metricCode = "";
  assert.equal(repoRunRequest(state, options), null);
  const command = ["cargo", "run", "--bin", "score"];
  const request = repoRunRequest(state, { ...options, metricCommand: command });
  assert.deepEqual(request?.scorer, {
    kind: "python",
    command_metric: true,
    timeout_seconds: state.workspace.metricTimeoutSeconds,
  });
  // The contract's command wins over wizard code, so the run scores with one metric.
  const withCode = repoRunRequest(repoState(), { ...options, metricCommand: command });
  assert.equal(withCode?.scorer.metric_code, undefined);
  assert.equal(withCode?.scorer.command_metric, true);
  // No command (a Python entry, or none) sends metric.py as today.
  const python = repoRunRequest(repoState(), { ...options, metricCommand: null });
  assert.equal(python?.scorer.command_metric, undefined);
  assert.ok(python?.scorer.metric_code);
});

test("the LiteLLM to BYOK map is the exact reverse of the billing bridge", () => {
  const reverse = Object.fromEntries(
    BYOK_PROVIDERS.map((p) => [litellmProviderForByok(p.slug), p.slug]).filter(
      ([prefix, slug]) => prefix !== slug,
    ),
  );
  assert.deepEqual({ ...LITELLM_TO_BYOK_PROVIDER }, reverse);
});

test("a local folder run sends provider local with its path, and runs in the local Docker box (SKY-993)", () => {
  const state = repoState();
  state.task.repo = { ...state.task.repo, localPath: " /Users/me/Documents/Skynet/app ", secrets: [] };
  state.optimizer.sandboxProvider = "vercel";
  state.workspace.files = { "src/app.py": "x = 2\n" };
  state.workspace.filesBase = "c".repeat(40);
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.deepEqual(request.target, {
    kind: "repo",
    repo: {
      provider: "local",
      path: "/Users/me/Documents/Skynet/app",
      editable_paths: ["src"],
      draft_files: { "src/app.py": "x = 2\n" },
    },
    setup_command: "npm ci",
  });
  assert.equal(request.sandbox_provider, "docker");
});

test("a local folder run sends the shell deletes that no draft writes again (SKY-997)", () => {
  const state = repoState();
  state.task.repo = { ...state.task.repo, localPath: "/Users/me/app", secrets: [] };
  state.workspace.files = { "src/app.py": "x = 2\n", "lib/new/a.py": "a = 1\n" };
  state.workspace.deleted = ["old.py", "src/app.py", "lib/new", "lib/new/a.py/x"];
  // A file swapped with a folder keeps its delete; the run deletes before it writes.
  assert.deepEqual(repoRunRequest(state, options)?.target.repo.draft_deleted, [
    "old.py",
    "lib/new",
    "lib/new/a.py/x",
  ]);

  state.workspace.deleted = ["src/app.py"];
  assert.equal("draft_deleted" in repoRunRequest(state, options)!.target.repo, false);
});

test("a repository run never sends shell deletes (SKY-997)", () => {
  const state = repoState();
  state.workspace.deleted = ["old.py"];
  assert.equal("draft_deleted" in repoRunRequest(state, options)!.target.repo, false);
});

test("a setup with only a chosen folder is worth keeping (SKY-993)", async () => {
  const { setupWorthKeeping } = await import("./session-state.ts");
  const state = initialWizardV2State();
  assert.equal(setupWorthKeeping(state), false);
  state.task.repo = { ...state.task.repo, localPath: "  " };
  assert.equal(setupWorthKeeping(state), false);
  state.task.repo = { ...state.task.repo, localPath: "/Users/me/app" };
  assert.equal(setupWorthKeeping(state), true);
});

test("a local run sends each imported .env secret by its saved id, with no host (SKY-994)", () => {
  const state = repoState();
  state.task.repo = {
    ...state.task.repo,
    localPath: "/Users/me/app",
    secrets: [{ name: "TYPED", value: "typed", allowedHosts: "api.example.com" }],
  };
  const request = repoRunRequest(state, options);
  assert.ok(request);
  const sent = withEnvSecrets(request, [
    { id: "s-typed-env", name: "TYPED" },
    { id: "s-db", name: "DB_URL" },
  ]);
  const secrets = sent.target.repo!.secrets!;
  assert.equal(secrets.length, 2);
  assert.equal(secrets[0].name, "TYPED");
  assert.deepEqual(secrets[1], { name: "DB_URL", saved_secret_id: "s-db", allowed_hosts: [] });
});

test("a repository run and an empty import keep the request as it is (SKY-994)", () => {
  const git = repoRunRequest(repoState(), options)!;
  assert.equal(withEnvSecrets(git, [{ id: "s-db", name: "DB_URL" }]), git);
  const state = repoState();
  state.task.repo = { ...state.task.repo, localPath: "/Users/me/app", secrets: [] };
  const local = repoRunRequest(state, options)!;
  assert.equal(withEnvSecrets(local, []), local);
});

test("only an explicit Use .env pick replaces a clashing saved secret (SKY-994)", () => {
  const repo = {
    ...emptyRepoDraft(),
    localPath: "/Users/me/app",
    envClashes: ["A", "B", "C"],
    envChoices: { A: "use" as const, B: "keep" as const },
  };
  assert.deepEqual(envReplaceNames(repo), ["A"]);
  assert.deepEqual(undecidedEnvClashes(repo), ["C"]);
  const moved = withLocalFolder(repo, "/Users/me/other");
  assert.equal(moved.envClashes, undefined);
  assert.equal(moved.envChoices, undefined);
  assert.deepEqual(withLocalFolder(repo, "/Users/me/app").envChoices, repo.envChoices);
});

test("a local run imports the folder's .env with the Use .env picks and sends the ids (SKY-994)", async () => {
  const state = repoState();
  state.task.repo = {
    ...state.task.repo,
    localPath: " /Users/me/app ",
    secrets: [],
    envClashes: ["A", "B"],
    envChoices: { A: "use", B: "keep" },
  };
  const request = repoRunRequest(state, options)!;
  const calls: Array<[string, string[]]> = [];
  const sent = await withLocalEnvImport(request, state.task.repo, async (path, replace) => {
    calls.push([path, replace]);
    return {
      kept: ["B"],
      secrets: [
        { id: "s-a", name: "A" },
        { id: "s-b", name: "B" },
        { id: "s-db", name: "DB_URL" },
      ],
    };
  });
  assert.deepEqual(calls, [["/Users/me/app", ["A"]]]);
  assert.deepEqual(sent.target.repo!.secrets, [
    { name: "A", saved_secret_id: "s-a", allowed_hosts: [] },
    { name: "B", saved_secret_id: "s-b", allowed_hosts: [] },
    { name: "DB_URL", saved_secret_id: "s-db", allowed_hosts: [] },
  ]);
});

test("a repository run never imports a .env (SKY-994)", async () => {
  const state = repoState();
  const request = repoRunRequest(state, options)!;
  let called = false;
  const sent = await withLocalEnvImport(request, state.task.repo, async () => {
    called = true;
    return { kept: [], secrets: [] };
  });
  assert.equal(called, false);
  assert.equal(sent, request);
});

test("the agent state lists only the .env clashes with no pick (SKY-994)", () => {
  const local = {
    ...emptyRepoDraft(),
    localPath: "/Users/me/app",
    envClashes: ["A", "B", "C"],
    envChoices: { A: "use" as const, B: "keep" as const },
  };
  assert.deepEqual(envSecretClashesState(local), ["C"]);
  assert.equal(envSecretClashesState({ ...local, envChoices: { ...local.envChoices, C: "keep" as const } }), undefined);
  assert.equal(envSecretClashesState({ ...local, localPath: undefined }), undefined);
});

test("a local run with more secrets than a run takes fails with the count before submit (SKY-994)", async () => {
  const state = repoState();
  state.task.repo = { ...state.task.repo, localPath: "/Users/me/app", secrets: [] };
  const request = repoRunRequest(state, options)!;
  const many = Array.from({ length: RUN_SECRETS_MAX + 1 }, (_, i) => ({ id: `s-${i}`, name: `K_${i}` }));
  await assert.rejects(
    withLocalEnvImport(request, state.task.repo, async () => ({ kept: [], secrets: many })),
    (err: unknown) => err instanceof TooManyRunSecretsError && err.count === RUN_SECRETS_MAX + 1,
  );
  const atLimit = await withLocalEnvImport(request, state.task.repo, async () => ({
    kept: [],
    secrets: many.slice(0, RUN_SECRETS_MAX),
  }));
  assert.equal(atLimit.target.repo!.secrets!.length, RUN_SECRETS_MAX);
});

test("a late .env clash stops the run start, joins the draft and reaches the agent state (SKY-994)", async () => {
  const state = repoState();
  state.task.repo = { ...state.task.repo, localPath: "/Users/me/app", secrets: [], envClashes: ["A"], envChoices: { A: "keep" } };
  const request = repoRunRequest(state, options)!;
  let submitted = false;
  let failure: ReturnType<typeof envSubmitFailure> = null;
  try {
    await withLocalEnvImport(request, state.task.repo, async () => ({ kept: ["A", "LATE"], secrets: [] }));
    submitted = true;
  } catch (err) {
    failure = envSubmitFailure(err, state.task.repo);
  }
  assert.equal(submitted, false);
  assert.ok(failure);
  assert.equal(failure.key, "submit.v2.repo.env.undecided");
  assert.deepEqual(failure.params, { names: "LATE" });
  assert.deepEqual(failure.repo!.envClashes, ["A", "LATE"]);
  assert.deepEqual(envSecretClashesState(failure.repo!), ["LATE"]);
  assert.equal(taskIssue({ ...state.task, repo: failure.repo! }), "submit.v2.repo.issue.env_clash");
});

test("a run start over the secret limit shows the count and keeps the draft (SKY-994)", () => {
  const repo = { ...emptyRepoDraft(), localPath: "/Users/me/app" };
  const failure = envSubmitFailure(new TooManyRunSecretsError(RUN_SECRETS_MAX + 1), repo);
  assert.deepEqual(failure, {
    key: "submit.v2.repo.env.too_many",
    params: { count: RUN_SECRETS_MAX + 1, limit: RUN_SECRETS_MAX },
  });
  assert.equal(envSubmitFailure(new Error("network"), repo), null);
});

test("a local run with an undecided .env clash does not import or submit (SKY-994)", async () => {
  const state = repoState();
  state.task.repo = {
    ...state.task.repo,
    localPath: "/Users/me/app",
    secrets: [],
    envClashes: ["A", "B"],
    envChoices: { A: "use" },
  };
  assert.equal(taskIssue(state.task), "submit.v2.repo.issue.env_clash");
  const request = repoRunRequest(state, options)!;
  let called = false;
  await assert.rejects(
    withLocalEnvImport(request, state.task.repo, async () => {
      called = true;
      return { kept: [], secrets: [] };
    }),
    (err: unknown) => err instanceof UndecidedEnvClashesError && err.names.join() === "B",
  );
  assert.equal(called, false);
  state.task.repo = { ...state.task.repo, envChoices: { A: "use", B: "keep" } };
  assert.equal(taskIssue(state.task), null);
});

test("a clash that the .env got after the preview stops the run with its name (SKY-994)", async () => {
  const state = repoState();
  state.task.repo = { ...state.task.repo, localPath: "/Users/me/app", secrets: [], envClashes: ["A"], envChoices: { A: "keep" } };
  const request = repoRunRequest(state, options)!;
  await assert.rejects(
    withLocalEnvImport(request, state.task.repo, async () => ({
      kept: ["A", "LATE"],
      secrets: [
        { id: "s-a", name: "A" },
        { id: "s-late", name: "LATE" },
      ],
    })),
    (err: unknown) => err instanceof UndecidedEnvClashesError && err.names.join() === "LATE",
  );
});
