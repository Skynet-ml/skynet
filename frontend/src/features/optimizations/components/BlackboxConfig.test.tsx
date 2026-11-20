/*
 * node --test cannot load .tsx, so this runs through jiti (BlackboxConfig.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { createRequire, registerHooks } from "node:module";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";
import type { OptimizationStatusResponse } from "@/shared/types/api";

registerHooks({
  load: (url, context, next) =>
    url.endsWith(".css")
      ? { format: "module", source: "", shortCircuit: true }
      : next(url, context),
});
const require = createRequire(import.meta.url);
// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { BlackboxConfigCard, runProposerHarness, runProposerLabel, runStopScore, shellJoin } = await import(
  "./BlackboxConfig"
);

const job = { optimization_id: "run-1", status: "running" } as OptimizationStatusResponse;

function render(payload: Record<string, unknown>): string {
  setClientLocale("en");
  return renderToStaticMarkup(
    <TooltipProvider>
      <BlackboxConfigCard job={job} payload={payload} />
    </TooltipProvider>,
  );
}

void describe("BlackboxConfigCard sandbox row", () => {
  void it("names the sandbox a run chose", () => {
    const html = render({ sandbox_provider: "docker" });
    // The UI catalog does not load under jiti, so its strings render as their keys.
    assert.match(html, /submit\.v2\.optimizer\.sandbox\.label/);
    assert.match(html, /submit\.runtime\.docker/);
  });

  void it("shows no sandbox row for a run that named none", () => {
    for (const payload of [{}, { sandbox_provider: null }, { sandbox_provider: "" }]) {
      assert.doesNotMatch(render(payload), /submit\.v2\.optimizer\.sandbox\.label/);
    }
  });
});

void describe("BlackboxConfigCard proposer rows", () => {
  // SummaryTabs opens on its first tab and a static render cannot click, so
  // this starts it on the Optimization tab, where the proposer rows are.
  function renderOptimizationTab(payload: Record<string, unknown>): string {
    const react = require("react") as { useState: typeof React.useState };
    const useState = react.useState;
    react.useState = ((initial: unknown) =>
      useState(initial === "general" ? "optimization" : initial)) as typeof React.useState;
    try {
      return render(payload);
    } finally {
      react.useState = useState;
    }
  }

  void it("shows the proposer settings of a repository run that staging has not read yet", () => {
    const html = renderOptimizationTab({
      target: { kind: "repo", repo: { repository: "o/r", editable_paths: ["."] } },
      proposer: {
        max_candidates_per_iter: 2,
        ralph: true,
        max_no_eval_seconds: 60,
        max_tool_calls: 5,
        install_command: "npm ci",
        run_command: "node agent.js",
      },
    });
    for (const key of [
      "submit.blackbox.proposer.max_candidates",
      "submit.blackbox.proposer.ralph",
      "optimization.config.proposer_idle",
      "submit.blackbox.proposer.max_tool_calls",
      "optimization.config.proposer_install",
      "optimization.config.proposer_run",
    ]) {
      assert.match(html, new RegExp(key.replaceAll(".", "\\.")));
    }
    assert.doesNotMatch(html, /submit\.blackbox\.review\.proposer</);
    const named = renderOptimizationTab({ proposer: { harness: "codex", max_tool_calls: 5 } });
    assert.match(named, /submit\.blackbox\.review\.proposer</);
  });
});

void describe("runProposerHarness", () => {
  const staged = {
    kind: "repo",
    repo: { repository: "o/r", editable_paths: ["."], environment: {} },
  } as never;
  const unstaged = { kind: "repo", repo: { repository: "o/r", editable_paths: ["."] } } as never;

  void it("is Codex when the run chose no harness and no contract command applies", () => {
    assert.equal(runProposerHarness({ max_tool_calls: 5 }, {}), "codex");
    assert.equal(runProposerHarness({ max_tool_calls: 5 }, staged), "codex");
  });

  void it("keeps the custom harness that staging wrote from the contract file", () => {
    assert.equal(
      runProposerHarness({ harness: "custom", run_command: "node agent.js" }, staged),
      "custom",
    );
  });

  void it("is custom when the stored environment record names the contract file's command", () => {
    // The stored shape: the worker saves the environment record, not the staged proposer.
    const fromFile = {
      kind: "repo",
      repo: {
        repository: "o/r",
        editable_paths: ["."],
        environment: { outcome: { target: null }, proposer_entry: ["node", "agent/run.js"] },
      },
    } as never;
    assert.equal(runProposerHarness({ max_tool_calls: 5 }, fromFile), "custom");
  });

  void it("is none for a repository run that staging has not read yet, or with no proposer", () => {
    assert.equal(runProposerHarness({}, unstaged), null);
    assert.equal(runProposerHarness(null, {}), null);
  });
});

void describe("runProposerLabel", () => {
  // The proposer row is on the Optimization tab, which a static render does not show.
  void it("names the contract file's command for a stored run that uses it", () => {
    setClientLocale("en");
    const fromFile = {
      kind: "repo",
      repo: {
        repository: "o/r",
        editable_paths: ["."],
        environment: { outcome: { target: null }, proposer_entry: ["node", "agent/run.js"] },
      },
    } as never;
    const html = renderToStaticMarkup(<>{runProposerLabel({ max_tool_calls: 5 }, fromFile)}</>);
    // The UI catalog does not load under jiti, so the row text is its key, not "Codex".
    assert.match(html, /submit\.blackbox\.review\.proposer_file/);
    // The command is isolated left-to-right text, so RTL text around it cannot reorder its punctuation.
    assert.match(html, /<bdi dir="ltr">node agent\/run\.js<\/bdi>/);
    assert.notEqual(html, renderToStaticMarkup(<>{runProposerLabel({ max_tool_calls: 5 }, {})}</>));
    assert.equal(runProposerLabel({ harness: "pi" }, fromFile), runProposerLabel({ harness: "pi" }, {}));
  });
});

void describe("shellJoin", () => {
  void it("quotes each part like Python's shlex.join, so the run page matches the run-record note", () => {
    // Expected values from Python: shlex.join([...]).
    assert.equal(shellJoin(["node", "agent/run.js", "--fast"]), "node agent/run.js --fast");
    assert.equal(shellJoin(["{model}", "a b", ""]), "'{model}' 'a b' ''");
    assert.equal(shellJoin(["it's", "x=1,y@2%3+4:5"]), `'it'"'"'s' x=1,y@2%3+4:5`);
    assert.equal(shellJoin(["$HOME", "é"]), "'$HOME' 'é'");
  });
});

void describe("runStopScore", () => {
  const repo = (outcome: unknown) =>
    ({ kind: "repo", repo: { repository: "o/r", editable_paths: ["."], environment: { outcome } } }) as never;

  void it("gives the outcome target the server recorded from ascent.toml", () => {
    assert.equal(runStopScore({ max_scorer_runs: 8 }, repo({ target: 0.9, require: {} })), 0.9);
  });

  void it("falls back to the retired budget stop score of a stored run", () => {
    assert.equal(runStopScore({ max_scorer_runs: 8, stop_at_score: 0.75 } as never, {}), 0.75);
  });

  void it("gives no stop score when the run has none", () => {
    assert.equal(runStopScore({}, {}), null);
    assert.equal(runStopScore({}, repo({ target: null, require: {} })), null);
    assert.equal(runStopScore({ stop_at_score: null } as never, {}), null);
  });
});
