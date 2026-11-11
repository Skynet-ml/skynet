/*
 * node --test cannot load .tsx, so this runs through jiti (composer-model-menu.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { MenuSide } from "@/features/local-runtime";
import { setClientLocale } from "@/shared/lib/runtime-locale";
import type { CatalogModel } from "@/shared/types/api";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { ComposerModelMenu, hiddenPickEffortStale } = await import("./composer-model-menu");

const SUBSCRIPTION: CatalogModel[] = [
  {
    value: "subscription/claude/anthropic/claude-sonnet-4.5",
    label: "Claude Sonnet 4.5",
    display_name: "Claude Sonnet 4.5",
    provider: "anthropic",
    supports_thinking: true,
    supports_vision: true,
    available: true,
  },
  {
    value: "subscription/claude/anthropic/claude-opus-4.1",
    label: "claude-opus-4.1",
    provider: "anthropic",
    supports_thinking: false,
    supports_vision: false,
    available: true,
  },
];

const TWO_CLIS: CatalogModel[] = [
  {
    value: "subscription/claude/anthropic/claude-sonnet-4.5",
    label: "Claude Sonnet 4.5",
    display_name: "Claude Sonnet 4.5",
    provider: "anthropic",
    supports_thinking: true,
    reasoning_efforts: ["low", "high"],
    supports_vision: true,
    available: true,
  },
  {
    value: "subscription/codex/openai/gpt-5",
    label: "GPT-5",
    display_name: "GPT-5",
    provider: "openai",
    supports_thinking: true,
    reasoning_efforts: ["low", "high", "max"],
    supports_vision: true,
    available: true,
  },
];

function render(
  value: string | null,
  models?: CatalogModel[],
  effort: string | null = null,
  side?: MenuSide,
): string {
  setClientLocale("en");
  return renderToStaticMarkup(
    <ComposerModelMenu
      value={value}
      onChange={() => {}}
      effort={effort}
      onEffortChange={() => {}}
      models={models}
      side={side}
    />,
  );
}

void describe("hiddenPickEffortStale", () => {
  const thinking = SUBSCRIPTION[0]!;
  const plain = SUBSCRIPTION[1]!;
  void it("sets aside a hidden pick's effort that the shown model does not take", () => {
    assert.equal(hiddenPickEffortStale(true, "max", thinking, ["low", "high"]), true);
    assert.equal(hiddenPickEffortStale(true, "high", plain, ["low", "high"]), true);
  });
  void it("sets aside a hidden pick's effort while the menu names no model", () => {
    assert.equal(hiddenPickEffortStale(true, "high", undefined, []), true);
  });
  void it("keeps an effort that fits and a listed pick's effort", () => {
    assert.equal(hiddenPickEffortStale(true, "high", thinking, ["low", "high"]), false);
    assert.equal(hiddenPickEffortStale(false, "max", thinking, ["low", "high"]), false);
    assert.equal(hiddenPickEffortStale(true, null, thinking, ["low"]), false);
  });
});

void describe("ComposerModelMenu", () => {
  void it("names the first listed model when the local build lists subscription models", () => {
    const html = render(null, SUBSCRIPTION);
    assert.match(html, />Claude Sonnet 4\.5</);
    // The chip shows the model's brand, not the subscription prefix.
    assert.doesNotMatch(html, /agent\.model_menu\.effort_default/);
  });

  void it("replaces a saved subscription pick without an effort slider", () => {
    const html = render("subscription/claude/anthropic/claude-opus-4.1", SUBSCRIPTION);
    assert.match(html, />Claude Sonnet 4\.5</);
  });

  void it("keeps the catalog default with no list, as in hosted builds", () => {
    // The catalog loads in an effect, so the first render names the default.
    // The UI catalog does not load under jiti, so its strings render as their keys.
    const html = render(null);
    assert.match(html, />agent\.model_menu\.effort_default</);
  });

  void it("names the effort that the request sends while no model is shown", () => {
    const html = render(null, undefined, "high");
    assert.match(html, />agent\.model_menu\.effort_high</);
  });

  void it("names a listed pick from the second CLI with its effort on the Local side", () => {
    const html = render("subscription/codex/openai/gpt-5", TWO_CLIS, "max", "local");
    assert.match(html, />GPT-5</);
    assert.match(html, />agent\.model_menu\.effort_max</);
  });

  void it("hides a Web pick on the Local side and sets aside an effort the shown model does not take", () => {
    const html = render("openrouter/google/gemini-3-pro", TWO_CLIS, "max", "local");
    assert.match(html, />Claude Sonnet 4\.5</);
    assert.doesNotMatch(html, /agent\.model_menu\.effort_/);
    // With no side the same pick is stale, so the effort is not set aside.
    assert.match(render("openrouter/google/gemini-3-pro", TWO_CLIS, "max"), /agent\.model_menu\.effort_high/);
  });

  void it("hides a CLI pick on the Web side", () => {
    const web = TWO_CLIS.slice(0, 1);
    const html = render("subscription/codex/openai/gpt-5", web, "max", "web");
    assert.match(html, />Claude Sonnet 4\.5</);
    assert.doesNotMatch(html, /GPT-5|agent\.model_menu\.effort_/);
  });
});
