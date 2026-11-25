/*
 * node --test cannot load .tsx, so this runs through jiti (chat-transcript.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { AgentMessage } from "./types";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { ChatTranscript } = await import("./chat-transcript");

const MESSAGES: AgentMessage[] = [
  { role: "user", content: "Write the intent" },
  { role: "assistant", content: "Wrote it." },
  { role: "user", content: "Now the metric" },
  { role: "assistant", content: "Writing the metric" },
];

function render(streaming: boolean): string {
  return renderToStaticMarkup(
    <ChatTranscript
      messages={MESSAGES}
      streaming={streaming}
      editAndResend={() => true}
      renderAfterContent={(msg) => <span data-edited-card="">{msg.content}</span>}
    />,
  );
}

function cards(html: string): string[] {
  return [...html.matchAll(/<span data-edited-card="">([^<]*)<\/span>/g)].map((m) => m[1] ?? "");
}

void describe("ChatTranscript after-content", () => {
  // SKY-786: the edited-files card shows only when its turn ends.
  void it("hides the streaming turn's after-content and keeps earlier turns'", () => {
    assert.deepEqual(cards(render(true)), ["Wrote it."]);
  });

  void it("shows every turn's after-content once the stream ends", () => {
    assert.deepEqual(cards(render(false)), ["Wrote it.", "Writing the metric"]);
  });
});
