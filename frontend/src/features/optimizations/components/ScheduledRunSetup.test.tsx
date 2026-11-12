/*
 * node --test cannot load .tsx, so this runs through jiti (ScheduledRunSetup.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";
import type { ContinuousEnableRequest } from "@/shared/types/continuous";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { ScheduledRunSetup } = await import("./ScheduledRunSetup");

function switches(initial?: ContinuousEnableRequest | null): Record<string, string> {
  setClientLocale("en");
  const html = renderToStaticMarkup(
    <ScheduledRunSetup
      idPrefix="t"
      finishLabel="continuous_tab.walk.save"
      onFinish={() => {}}
      initial={initial}
    />,
  );
  const states: Record<string, string> = {};
  for (const tag of html.match(/<button[^>]*role="switch"[^>]*>/g) ?? []) {
    const role = /aria-labelledby="t-([a-z_]+)"/.exec(tag)?.[1];
    const checked = /aria-checked="(true|false)"/.exec(tag)?.[1];
    if (role && checked) states[role] = checked;
  }
  return states;
}

void describe("ScheduledRunSetup", () => {
  void it("shows only the reflection model switch, on for a new plan", () => {
    assert.deepEqual(switches(), { reflection_model: "true" });
  });

  void it("keeps a switch the user turned off when a saved plan is edited again", () => {
    const saved: ContinuousEnableRequest = {
      schedule: {
        frequency: "daily",
        every: 1,
        time: "06:30",
        weekdays: [0],
        month_day: 1,
        timezone: "UTC",
      },
      permitted: { task_model: true, reflection_model: false },
    };
    assert.deepEqual(switches(saved), { reflection_model: "false" });
  });
});
