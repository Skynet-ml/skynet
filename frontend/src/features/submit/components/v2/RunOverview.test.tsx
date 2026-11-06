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

function render(state: WizardV2State): string {
  setClientLocale("en");
  return renderToStaticMarkup(
    <TooltipProvider>
      <RunOverview state={state} cc={cc} guided={false} />
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
  const repoState = () => {
    const state = initialWizardV2State("expert");
    state.task.repo = {
      repository: "acme/agent",
      branch: "main",
      commit: "",
      editablePaths: ["."],
      setupCommand: "",
    };
    return state;
  };

  void it("lets a plain repository run start on the web", () => {
    assert.equal(hostedRunBlock(repoState(), [{ name: "openai/gpt-5" }]), null);
  });

  void it("keeps every Mac-only input off the web", () => {
    assert.equal(
      hostedRunBlock(initialWizardV2State("expert"), []),
      "submit.v2.run_on.blocked.repo_only",
    );
    const cli = repoState();
    assert.equal(
      hostedRunBlock(cli, [
        { name: "openai/gpt-5", token_source: "subscription", cli_provider: "codex" },
      ]),
      "submit.v2.run_on.blocked.cli_model",
    );
    const proposer = repoState();
    proposer.optimizer.proposer = { ...proposer.optimizer.proposer, host_cli: "claude" };
    assert.equal(hostedRunBlock(proposer, []), "submit.v2.run_on.blocked.host_cli");
    const staged = repoState();
    staged.task.needsData = true;
    staged.inputs.stagedDatasetId = "staged-1";
    assert.equal(hostedRunBlock(staged, []), "submit.v2.run_on.blocked.staged_data");
    staged.inputs.cases = [{ question: "2+2" }] as unknown as typeof staged.inputs.cases;
    assert.equal(hostedRunBlock(staged, []), null);
  });
});
