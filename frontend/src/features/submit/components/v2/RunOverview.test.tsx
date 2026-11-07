/*
 * node --test cannot load .tsx, so this runs through jiti (RunOverview.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";
import type { ContractCheckState } from "../../hooks/use-contract-check";
import type { BlackboxRunRequest, CatalogModel } from "@/shared/types/api";
import type { HardeningJob, HardeningStatus } from "@/shared/types/hardening";
import { initialWizardV2State, type WizardV2State } from "../../lib/wizard-v2";

// The model picker this screen reuses imports a stylesheet, which Node cannot load.
registerHooks({
  load: (url, context, next) =>
    url.endsWith(".css")
      ? { format: "module", source: "", shortCircuit: true }
      : next(url, context),
});
// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { RunOverview } = await import("./RunOverview");
const { hostedRunBlock } = await import("../../hooks/use-unified-wizard");

const cc: ContractCheckState = {
  staticResult: null,
  staticPending: false,
  lastCheck: null,
  stale: false,
  checking: false,
  error: null,
  timing: null,
  terminal: null,
  canCheck: true,
  check: async () => null,
};

function render(state: WizardV2State, hardening: HardeningJob | null = null): string {
  setClientLocale("en");
  return renderToStaticMarkup(
    <TooltipProvider>
      <RunOverview state={state} cc={cc} guided={false} hardening={hardening} />
    </TooltipProvider>,
  );
}

void describe("RunOverview", () => {
  void it("sums up the run in four tabs and opens on General", () => {
    const state = initialWizardV2State("expert");
    state.task.objective = "Classify support tickets";
    const html = render(state);
    // The UI catalog does not load under jiti, so its strings render as their keys.
    assert.equal(html.match(/role="tab"/g)?.length, 4);
    for (const tab of ["general", "data", "models", "budget"]) {
      assert.match(html, new RegExp(`submit\\.v2\\.overview\\.tab\\.${tab}<`));
    }
    assert.match(html, /aria-selected="true"[^>]*>.*?submit\.v2\.overview\.tab\.general</);
    assert.match(html, /Classify support tickets/);
    // The contract check gates Submit, so it shows on every tab.
    assert.match(html, /submit\.v2\.metric\.check\.needed/);
    // Each tab carries its icon, as the old summary step did.
    assert.equal(html.match(/role="tab"[^>]*>\s*<svg/g)?.length, 4);
    // General restores the old summary's optimizer, visibility and billing rows.
    for (const key of [
      "submit.v2.stage.optimizer",
      "submit.basics.privacy.label",
      "submit.budget.billing_source",
    ]) {
      assert.match(html, new RegExp(key.replace(/\./g, "\\.")));
    }
  });

  void it("adds a reward tab when the metric declares REWARD_SPEC", () => {
    const state = initialWizardV2State("expert");
    state.workspace.metricCode =
      "REWARD_SPEC = {'weights': {'accuracy': 1, 'speed': 1, 'cost': 1}, 'speed': {'reference': 500}, 'cost': {'reference': 0.01}}\n";
    const html = render(state);
    assert.equal(html.match(/role="tab"/g)?.length, 5);
    assert.match(html, /submit\.v2\.overview\.tab\.reward</);
    state.workspace.metricCode = "def score(example, output):\n    return 1.0\n";
    assert.doesNotMatch(render(state), /submit\.v2\.overview\.tab\.reward</);
  });

  void it("drops the data tab for a repository run", () => {
    const state = initialWizardV2State("expert");
    state.task.repo = {
      repository: "acme/agent",
      branch: "main",
      commit: "",
      editablePaths: ["."],
      setupCommand: "",
    };
    const html = render(state);
    assert.equal(html.match(/role="tab"/g)?.length, 3);
    assert.doesNotMatch(html, /submit\.v2\.overview\.tab\.data</);
    assert.match(html, /acme\/agent @ main/);
    assert.match(html, /submit\.v2\.repo\.check_on_submit/);
  });

  void it("names the subscription as the billing source when every model runs on one", () => {
    const state = initialWizardV2State("expert");
    state.optimizer.taskModel = {
      name: "anthropic/claude-haiku-4.5",
      token_source: "subscription",
      cli_provider: "claude",
    };
    state.optimizer.reflectionModel = {
      name: "openai/gpt-5",
      token_source: "subscription",
      cli_provider: "codex",
    };
    const html = render(state);
    assert.match(html, /subscription\.mode</);
    assert.doesNotMatch(html, /billing\.mode\.managed/);
    // One managed model puts the run back on the balance.
    state.optimizer.reflectionModel = { name: "openai/gpt-5" };
    const mixed = render(state);
    assert.match(mixed, /billing\.mode\.managed</);
    assert.doesNotMatch(mixed, /subscription\.mode</);
  });

  void it("shows no Mac or Web choice outside a linked Mac's wizard", () => {
    const state = initialWizardV2State("expert");
    assert.doesNotMatch(render(state), /submit\.v2\.run_on\./);
  });
});

void describe("hostedRunBlock", () => {
  const request = (extra: Partial<BlackboxRunRequest> = {}) =>
    ({
      task_model_config: null,
      reflection_model_config: { name: "openai/gpt-5" },
      extra_reflection_model_configs: [],
      scorer: { model: null },
      proposer: null,
      staged_dataset_id: null,
      ...extra,
    }) as unknown as BlackboxRunRequest;
  const block = (over: Partial<Parameters<typeof hostedRunBlock>[0]> = {}) =>
    hostedRunBlock({ repo: true, request: request(), linked: true, catalog: "ready", ...over });
  const cliModel = { name: "openai/gpt-5", token_source: "subscription", cli_provider: "codex" };

  void it("lets a plain repository run start on the web", () => {
    assert.equal(block(), null);
  });

  void it("stops a web run when the Mac lost its link", () => {
    assert.equal(block({ linked: false }), "submit.v2.run_on.blocked.unlinked");
  });

  void it("keeps a web run waiting for the web catalog", () => {
    assert.equal(block({ catalog: "loading" }), "submit.v2.run_on.blocked.catalog_loading");
    assert.equal(block({ catalog: "failed" }), "submit.v2.run_on.blocked.catalog_failed");
  });

  void it("judges the models the request sends", () => {
    assert.equal(block({ repo: false }), "submit.v2.run_on.blocked.repo_only");
    assert.equal(
      block({ request: request({ scorer: { model: cliModel } } as Partial<BlackboxRunRequest>) }),
      "submit.v2.run_on.blocked.cli_model",
    );
    assert.equal(
      block({
        request: request({
          extra_reflection_model_configs: [cliModel],
        } as Partial<BlackboxRunRequest>),
      }),
      "submit.v2.run_on.blocked.cli_model",
    );
    assert.equal(
      block({
        request: request({ reflection_model_config: cliModel } as Partial<BlackboxRunRequest>),
      }),
      "submit.v2.run_on.blocked.cli_model",
    );
  });

  void it("keeps the other Mac-only inputs off the web", () => {
    assert.equal(
      block({
        request: request({ proposer: { host_cli: "claude" } } as Partial<BlackboxRunRequest>),
      }),
      "submit.v2.run_on.blocked.host_cli",
    );
    assert.equal(
      block({ request: request({ staged_dataset_id: "staged-1" }) }),
      "submit.v2.run_on.blocked.staged_data",
    );
  });
});

function hardeningJob(status: HardeningStatus): HardeningJob {
  const exploit = {
    kind: "case" as const,
    label: "valid" as const,
    confidence: 0.9,
    reason: "",
    unsure: false,
    patch_diff: "",
    why_md: "",
    user_verdict: null,
  };
  return {
    id: "job-1",
    status,
    round: 2,
    rounds: 5,
    round_cap_usd: 2,
    reserved_usd: 10,
    spent_usd: 1.2,
    place: "cloud",
    attacker_model: "a",
    judge_model: "j",
    view: "black_box",
    preflight_failures: [],
    rounds_detail: [
      { index: 1, status: "fixed", exploits: [{ ...exploit, id: "e1" }, { ...exploit, id: "e2" }], fix: null },
      { index: 2, status: "clean", exploits: [{ ...exploit, id: "e3" }], fix: null },
    ],
    suite_count: 1,
    fixed_count: 1,
    error: null,
  };
}

void describe("RunOverview hardening line", () => {
  void it("names the hardening only when the job ended well", () => {
    const state = initialWizardV2State("expert");
    assert.doesNotMatch(render(state), /hardening_survived/);
    for (const status of ["clean_stop", "budget_stop", "done"] as const) {
      assert.match(render(state, hardeningJob(status)), /submit\.v2\.overview\.hardening_survived/);
    }
    for (const status of ["running", "waiting_review", "failed", "rejected"] as const) {
      assert.doesNotMatch(render(state, hardeningJob(status)), /hardening_survived/);
    }
  });
});

const { hardeningSurvived } = await import("@/shared/types/hardening");
const { hardeningDefaultModel } = await import("../../hooks/use-hardening");
const { subscriptionAgentModel } = await import("@/features/local-runtime");

type Exploit = HardeningJob["rounds_detail"][number]["exploits"][number];
function claim(id: string, label: Exploit["label"], user_verdict: Exploit["user_verdict"]): Exploit {
  return { id, kind: "case", label, confidence: 0.9, reason: "", unsure: false, patch_diff: "", why_md: "", user_verdict };
}

void describe("hardeningSurvived", () => {
  void it("counts nothing for a clean stop with no claims, and the overview says the round was clean", () => {
    const job = { ...hardeningJob("clean_stop"), round: 1, fixed_count: 0, suite_count: 0 };
    job.rounds_detail = [{ index: 1, status: "clean", exploits: [], fix: null }];
    assert.equal(hardeningSurvived(job), 0);
    const html = render(initialWizardV2State("expert"), job);
    assert.doesNotMatch(html, /hardening_survived/);
    assert.match(html, /submit\.v2\.harden\.clean/);
  });

  void it("skips confirmed exploits whose round never got its fix at a budget stop", () => {
    const job = hardeningJob("budget_stop");
    job.rounds_detail = [
      { index: 1, status: "waiting_fix", exploits: [claim("a", "exploit", "confirmed"), claim("b", "exploit", "confirmed")], fix: null },
    ];
    assert.equal(hardeningSurvived(job), 0);
    const html = render(initialWizardV2State("expert"), job);
    assert.match(html, /submit\.v2\.harden\.budget/);
  });

  void it("counts valid, rejected and fixed claims in a mixed job", () => {
    const job = hardeningJob("done");
    job.rounds_detail = [
      {
        index: 1,
        status: "fixed",
        exploits: [claim("a", "exploit", "confirmed"), claim("b", "exploit", "rejected"), claim("c", "valid", null)],
        fix: null,
      },
      {
        index: 2,
        status: "waiting_fix",
        exploits: [claim("d", "exploit", "confirmed"), claim("e", "valid", "rejected"), claim("f", "exploit", null)],
        fix: null,
      },
    ];
    // a (fixed), b (rejected), c (valid), e (rejected); d waits on a fix, f is an unreviewed exploit.
    assert.equal(hardeningSurvived(job), 4);
    assert.match(render(initialWizardV2State("expert"), job), /submit\.v2\.overview\.hardening_survived/);
  });
});

void describe("hardeningDefaultModel", () => {
  const base = { provider: "p", supports_thinking: false, supports_vision: false, available: true };
  const first: CatalogModel = { ...base, value: "first", label: "First" };
  const models: CatalogModel[] = [first, { ...base, value: "catalog-default", label: "Default", is_default: true }];

  void it("keeps a managed or untagged reflection model", () => {
    assert.equal(hardeningDefaultModel({ name: "m1", token_source: "managed" }, models, false, null), "m1");
    assert.equal(hardeningDefaultModel({ name: "m2" }, models, false, null), "m2");
  });

  void it("maps a local subscription model through its CLI", () => {
    const want = subscriptionAgentModel("sonnet", "claude", null);
    assert.ok(want);
    assert.equal(
      hardeningDefaultModel({ name: "sonnet", token_source: "subscription", cli_provider: "claude" }, models, true, null),
      want,
    );
  });

  void it("falls back to the catalog default for BYOK, hosted subscription, empty or missing picks", () => {
    const sub = { name: "sonnet", token_source: "subscription" as const, cli_provider: "claude" as const };
    assert.equal(hardeningDefaultModel({ name: "k", token_source: "byok" }, models, false, null), "catalog-default");
    assert.equal(hardeningDefaultModel(sub, models, false, null), "catalog-default");
    assert.equal(hardeningDefaultModel({ name: "  ", token_source: "managed" }, models, false, null), "catalog-default");
    assert.equal(hardeningDefaultModel(null, models, false, null), "catalog-default");
  });

  void it("uses the first model when the catalog marks no default", () => {
    assert.equal(hardeningDefaultModel(null, [first], false, null), "first");
    assert.equal(hardeningDefaultModel(null, [], false, null), "");
  });
});
