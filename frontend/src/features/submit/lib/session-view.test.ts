import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

import { reopenedPage } from "./session-state.ts";

const unifiedRunUrl = new URL("../../../shared/types/unified-run.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/types/unified-run") {
      return { shortCircuit: true, url: unifiedRunUrl };
    }
    return nextResolve(specifier, context);
  },
});

const { initialWizardV2State } = await import("./wizard-v2.ts");
const { stepPages } = await import("./wizard-pages.ts");

test("a reload reopens the screen it was left on inside the step", () => {
  const state = {
    ...initialWizardV2State("guided"),
    step: "metric" as const,
    furthestStep: "metric" as const,
  };
  const screens = stepPages("metric", state);
  assert.equal(screens[reopenedPage(screens, "check")], "check");
  assert.equal(screens[reopenedPage(screens, "overview")], "overview");
  // A screen the step no longer has, or none saved, opens the step's first.
  assert.equal(reopenedPage(screens, "budget"), 0);
  assert.equal(reopenedPage(screens, null), 0);
});
