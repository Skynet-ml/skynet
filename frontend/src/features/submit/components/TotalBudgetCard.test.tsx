import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

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
const { TotalBudgetCard } = await import("./TotalBudgetCard");

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
