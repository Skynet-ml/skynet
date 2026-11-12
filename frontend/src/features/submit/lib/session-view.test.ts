import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const unifiedRunUrl = new URL("../../../shared/types/unified-run.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/types/unified-run") {
      return { shortCircuit: true, url: unifiedRunUrl };
    }
    if (specifier.startsWith("./") && !specifier.endsWith(".ts") && context.parentURL) {
      return { shortCircuit: true, url: new URL(`${specifier}.ts`, context.parentURL).href };
    }
    return nextResolve(specifier, context);
  },
});

const { initialWizardV2State } = await import("./wizard-v2.ts");
const { stepPages } = await import("./wizard-pages.ts");
const { reopenedPage } = await import("./session-state.ts");

test("a reload reopens the screen it was left on inside the step", () => {
  const state = {
    ...initialWizardV2State("guided"),
    step: "metric" as const,
    furthestStep: "metric" as const,
  };
  const screens = stepPages("metric", state);
  assert.equal(screens[reopenedPage(screens, "harden")], "harden");
  assert.equal(screens[reopenedPage(screens, "overview")], "overview");
  // A screen the step no longer has (the retired sandbox check), or none saved, opens the step's first.
  assert.equal(reopenedPage(screens, "check"), 0);
  assert.equal(reopenedPage(screens, null), 0);
});
