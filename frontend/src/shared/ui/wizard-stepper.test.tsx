/*
 * node --test cannot load .tsx, so this runs through jiti (wizard-stepper.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { Database, Lightbulb } from "@/shared/ui/icons";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { WizardStepper } = await import("./wizard-stepper");

/** Render the stepper on its second step, with the first step done. */
function render(steps: ReadonlyArray<{ id: string; label: string; icon?: typeof Lightbulb }>) {
  return renderToStaticMarkup(
    <WizardStepper
      steps={steps}
      step={1}
      maxReachableStep={1}
      validateStep={() => true}
      onSelect={() => {}}
    />,
  );
}

/** Return the inner HTML of each step button. */
function buttons(html: string): string[] {
  return [...html.matchAll(/<button[^>]*>(.*?)<\/button>/gs)].map((m) => m[1] ?? "");
}

void describe("WizardStepper", () => {
  void it("shows each step's icon in place of its number, filled once the step is done", () => {
    const [done = "", active = ""] = buttons(
      render([
        { id: "task", label: "Task", icon: Lightbulb },
        { id: "inputs", label: "Inputs", icon: Database },
      ]),
    );
    for (const inner of [done, active]) {
      assert.match(inner, /<svg/);
      assert.doesNotMatch(inner, />\s*[12]\s*</);
    }
    // The done step draws the filled weight, as the setup draft dialog does.
    assert.equal(
      done.match(/<svg.*<\/svg>/s)?.[0],
      renderToStaticMarkup(<Lightbulb className="size-5" weight="fill" aria-hidden />),
    );
    assert.equal(
      active.match(/<svg.*<\/svg>/s)?.[0],
      renderToStaticMarkup(<Database className="size-5" weight="bold" aria-hidden />),
    );
  });

  void it("shows numbers and a check mark for steps without an icon", () => {
    const [done = "", active = ""] = buttons(
      render([
        { id: "a", label: "A" },
        { id: "b", label: "B" },
      ]),
    );
    assert.match(done, /<svg/);
    assert.match(active, /^2</);
  });
});
