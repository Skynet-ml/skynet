/*
 * node --test cannot load .tsx, so this runs through jiti (chat-notices.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { setClientLocale } from "@/shared/lib/runtime-locale";
import { setServerMessages } from "@/shared/lib/runtime-messages";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { ModelChangedNotice } = await import("./chat-notices");

setServerMessages(buildActiveCatalog("en"), "en");
setClientLocale("en");

void describe("ModelChangedNotice", () => {
  void it("isolates each model name, so an RTL sentence keeps it whole", () => {
    const html = renderToStaticMarkup(
      <ModelChangedNotice fromModel="Llama 3.1 70B Instruct (free)" toModel="GPT-5" />,
    );
    assert.ok(html.includes("⁨Llama 3.1 70B Instruct (free)⁩"), html);
    assert.ok(html.includes("⁨GPT-5⁩"), html);
  });
});
