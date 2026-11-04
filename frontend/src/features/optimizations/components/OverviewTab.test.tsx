/*
 * node --test cannot load .tsx, so this runs through jiti (OverviewTab.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS='{"@/":"<abs>/src/","react-loading-skeleton/dist/skeleton.css":"<abs>/src/features/optimizations/components/__fixtures__/style-stub.ts"}' npx jiti <this file>
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { OverviewTab } = await import("./OverviewTab");
const { GridOverview } = await import("./GridOverview");
const { OVERVIEW_FIXTURES, overviewFixture } = await import("./__fixtures__/overview");

type Fixture = (typeof OVERVIEW_FIXTURES)[number];

function render(fixture: Fixture): string {
  const inner = fixture.expect.grid ? (
    <GridOverview job={fixture.job} onPairSelect={() => {}} />
  ) : (
    <OverviewTab
      job={fixture.job}
      isActive={false}
      scorePoints={fixture.scorePoints}
      activePairIndex={null}
      onPairSelect={() => {}}
      payload={fixture.payload}
    />
  );
  return renderToStaticMarkup(<TooltipProvider>{inner}</TooltipProvider>);
}

// The score cards keep the current theme's colours: plain baseline card, the
// on-primary optimized card, a muted card for a non-negative change.
const BASELINE_CARD = "rounded-xl border border-border/50 bg-card p-6 text-center";
const OPTIMIZED_CARD = "rounded-xl border on-primary p-6 text-center";
const IMPROVEMENT_CARD = "border-border bg-muted/50";

void describe("finished-run Overview fixtures", () => {
  for (const fixture of OVERVIEW_FIXTURES) {
    void it(`${fixture.name} renders the Overview`, () => {
      const html = render(fixture);
      const { expect } = fixture;

      assert.ok(!html.includes("optimization.result."), "no Guided result card copy");

      if (!expect.grid) {
        assert.ok(html.includes("data-stage-tracker"), "pipeline stages");
      }

      for (const cls of [BASELINE_CARD, OPTIMIZED_CARD, IMPROVEMENT_CARD]) {
        assert.equal(html.includes(cls), expect.scoreCards, `score card class: ${cls}`);
      }

      const loggedRows = html.match(/<th scope="row"/g)?.length ?? 0;
      assert.equal(loggedRows, expect.loggedMetricRows, "logged-metric rows");

      assert.ok(
        !html.includes("auto.features.optimizations.components.overviewtab.1<"),
        "no LM call count card",
      );
      assert.ok(!html.includes("run-overview-classic"), "no scoped 10-09 palette");
      assert.ok(!html.includes("optimization.blackbox.stats.scorer_runs"), "no scorer-run card");
      assert.equal(/stage\.hillClimbing/i.test(html), expect.climb, "hill-climb stage");

      if (expect.grid) {
        for (const pair of fixture.job.grid_result?.pair_results ?? []) {
          assert.ok(html.includes(pair.generation_model.split("/").pop() ?? ""), "pair model");
        }
      }
    });
  }

  void it("shows the score progression card only when there is a series", () => {
    assert.ok(render(overviewFixture("dspy-single-module")).includes("overviewtab.4"));
    assert.ok(!render(overviewFixture("dspy-no-baseline")).includes("overviewtab.4"));
  });

  void it("formats DSPy scores as percentages and black-box scores raw", () => {
    const dspy = render(overviewFixture("dspy-single-module"));
    assert.ok(dspy.includes(">42.0%<") || dspy.includes(">42%<"), "DSPy baseline percent");
    const html = render(overviewFixture("oa-gepa"));
    assert.ok(html.includes(">0.42<") && html.includes(">0.78<"), "raw black-box scores");
  });
});

void describe("detail page result layout", () => {
  const dir = path.dirname(new URL(import.meta.url).pathname);

  void it("no longer ships the Guided result card", () => {
    assert.ok(!fs.existsSync(path.join(dir, "RunResultCard.tsx")));
    const view = fs.readFileSync(path.join(dir, "OptimizationDetailView.tsx"), "utf8");
    assert.ok(!view.includes("RunResultCard"));
    assert.ok(!view.includes("resultCard"));
  });

  void it("shows the not-held-out notice once, just above the run summary card", () => {
    const view = fs.readFileSync(path.join(dir, "OptimizationDetailView.tsx"), "utf8");
    assert.equal(view.match(/<HeldOutNotice /g)?.length, 1);
    const notice = view.indexOf("<HeldOutNotice ");
    const summary = view.indexOf(
      '<div className="rounded-xl border border-border/40 bg-card p-4 sm:p-5">',
    );
    assert.ok(notice > 0 && notice < summary, "notice renders before the summary card");
    assert.ok(!view.slice(notice, summary).includes("<div"), "nothing else sits between them");
    const gate = view.lastIndexOf("{jobIsBlackbox", notice);
    assert.ok(
      view.slice(gate, notice).includes("isNotHeldOut("),
      "the FadeIn wrapper renders only with the notice, so no empty gap sits above the summary",
    );

    const fixture = overviewFixture("oa-gepa");
    const job = {
      ...fixture.job,
      blackbox_result: {
        ...fixture.job.blackbox_result!,
        split_counts: {
          train: 2,
          val: 1,
          test: 0,
          held_out: false,
          selection_split: "val",
          report_split: "val",
        },
      },
    } as unknown as Fixture["job"];
    assert.ok(!render({ ...fixture, job }).includes("not-held-out"), "Overview drops the notice");
  });
});
