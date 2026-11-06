/*
 * node --test cannot load .tsx, so this runs through jiti (ServeTab.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS='{"@/":"<abs>/src/","react-loading-skeleton/dist/skeleton.css":"<abs>/src/features/optimizations/components/__fixtures__/style-stub.ts"}' npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { ServeTabBody } = await import("./ServeTab");
const { buildServeSnippets } = await import("./ServeCodeSnippets");
const { getRuntimeEnv } = await import("@/shared/lib/runtime-env");
const { msg } = await import("@/shared/lib/messages");

const noop = () => {};
const render = (enabled: boolean) =>
  renderToStaticMarkup(
    <ServeTabBody
      optimizationId="opt-1"
      enabled={enabled}
      toggling={false}
      onToggle={noop}
      draft=""
      onDraftChange={noop}
      running={false}
      onRun={noop}
      history={[]}
    />,
  );
const escape = (text: string) =>
  text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

void describe("ServeTabBody", () => {
  void it("shows only the switch and the opt-in hint while serving is off", () => {
    const html = render(false);
    assert.match(html, /role="switch"/);
    assert.match(html, /aria-checked="false"/);
    assert.ok(html.includes(escape(msg("serve_tab.off_hint"))));
    assert.ok(html.includes(escape(msg("serve_tab.requirement"))));
    assert.doesNotMatch(html, /<textarea/);
    assert.ok(!html.includes("/serve/opt-1"));
  });

  void it("shows the playground, the endpoint URL and the snippets while serving is on", () => {
    const html = render(true);
    assert.match(html, /aria-checked="true"/);
    assert.match(html, /<textarea[^>]*id="serve-playground-input"/);
    assert.ok(html.includes(escape(msg("serve_tab.playground.empty"))));
    const url = `${getRuntimeEnv().apiUrl}/serve/opt-1`;
    assert.ok(html.includes(url));
    assert.match(html, /dir="ltr"/);
    assert.ok(html.includes(escape(msg("serve_tab.api.copy"))));
    // The lazy code editor reserves its geometry while CodeMirror loads.
    assert.match(html, /--editor-loading-height/);
  });
});

void describe("buildServeSnippets", () => {
  const url = "https://api.example.test/serve/opt-1";
  const snippets = buildServeSnippets(url);

  void it("offers cURL, Python, JavaScript and Go, and no DSPy", () => {
    assert.deepEqual(Object.keys(snippets).sort(), ["curl", "go", "javascript", "python"]);
    for (const code of Object.values(snippets)) assert.doesNotMatch(code, /dspy/i);
  });

  void it("authenticates with the API token, sends an idempotency key and an input field", () => {
    for (const code of Object.values(snippets)) {
      assert.ok(code.includes(url));
      assert.ok(code.includes("APP_API_TOKEN"));
      assert.ok(code.includes("Idempotency-Key"));
      assert.ok(code.includes("Settings → API"));
      assert.match(code, /inputs/);
      assert.match(code, /"?input"?: ?"Hello"/);
    }
    assert.ok(snippets.curl.includes('-H "Authorization: Bearer $APP_API_TOKEN"'));
    assert.ok(snippets.curl.includes(`'{"inputs":{"input":"Hello"}}'`));
  });
});
