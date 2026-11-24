import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { msg } from "@/shared/lib/messages";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { setClientLocale } from "@/shared/lib/runtime-locale";
import { setServerMessages } from "@/shared/lib/runtime-messages";
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";

(globalThis as { React?: typeof React }).React = React;
registerHooks({
  load(url, context, nextLoad) {
    if (url.endsWith(".css")) return { format: "module", source: "", shortCircuit: true };
    return nextLoad(url, context);
  },
});
const { ExecutionBudgetProvider } = await import("../hooks/use-execution-budget");
const { TotalBudgetCard, limitExplainerKey, runLedger } = await import("./TotalBudgetCard");

setServerMessages(buildActiveCatalog("en"), "en");
setClientLocale("en");

void test("BYOK budget card renders without a model fee line", () => {
  const html = renderToStaticMarkup(
    <TooltipProvider>
      <ExecutionBudgetProvider>
        <TotalBudgetCard
          mode="byok"
          w={{
            runFloor: null,
            maxCostCents: 100,
            setMaxCostCents: () => {},
            budgetUncapped: false,
            setBudgetUncapped: () => {},
          }}
        />
      </ExecutionBudgetProvider>
    </TooltipProvider>,
  );
  assert.match(html, /id="totalBudgetInput"/);
  assert.match(html, /charges no model fee/);
  assert.match(html, /budget covers sandbox usage only/);
  assert.doesNotMatch(html, /Estimated platform fee/);
});

void test("the budget ledger counts the run's own spend and holds, not the setup check's", () => {
  const budget = {
    id: "b1",
    total_cents: 500,
    revision: 1,
    generation: 0,
    state: "open",
    job_id: null,
    setup_spent_cents: "60",
    run_spent_cents: "0",
    reserved_cents: "12.5",
    available_cents: "500",
    billed_cents: 60,
    wallet_setup_spent_cents: "60",
    wallet_run_spent_cents: "0",
    wallet_reserved_cents: 13,
    account_available_cents: 1000,
    external_spent_cents: "0",
    pending_operations: 1,
    blocked_reason: null,
    uncapped: false,
  };
  assert.deepEqual(runLedger(budget), { spent: "0", reserved: "0" });
  assert.deepEqual(runLedger({ ...budget, job_id: "job-1", run_spent_cents: "40.25" }), {
    spent: "40.25",
    reserved: "12.5",
  });
});

void test("the limit tooltip says setup counts in the limit only when the account turned off wallet setup billing", () => {
  assert.equal(limitExplainerKey(true), "submit.budget.explainer");
  assert.equal(limitExplainerKey(null), "submit.budget.explainer");
  assert.equal(limitExplainerKey(false), "submit.budget.explainer.setup_in_limit");
  assert.match(msg(limitExplainerKey(true)), /paid from your balance/);
  assert.match(msg(limitExplainerKey(false)), /count against this limit/);
});
