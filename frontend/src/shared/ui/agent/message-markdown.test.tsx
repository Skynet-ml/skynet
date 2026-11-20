/*
 * node --test cannot load .tsx, so this runs through jiti (message-markdown.render.test.ts
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
const { MessageMarkdown } = await import("./message-markdown");

setServerMessages(buildActiveCatalog("en"), "en");
setClientLocale("en");

const render = (content: string, tone?: "user-message", streaming?: boolean) =>
  renderToStaticMarkup(<MessageMarkdown content={content} tone={tone} streaming={streaming} />);

const count = (html: string, part: string) => html.split(part).length - 1;

void describe("MessageMarkdown", () => {
  void it("ships its stylesheet once, before the message", () => {
    const html = render("text");
    assert.ok(html.startsWith("<style"), html);
    assert.ok(html.includes('data-href="skynet-message-markdown"'), html);
    assert.ok(html.includes(".md-root > :first-child"), "the CSS text is not escaped");
  });

  void it("keeps heading levels", () => {
    const html = render("# One\n\n## Two");
    assert.ok(html.includes("<h1") && html.includes("<h2"), html);
  });

  void it("gives a code block a language title, a wrap toggle and a copy button", () => {
    const html = render("```html\n<p>x</p>\n```\n\n```\nplain\n```");
    assert.ok(html.includes(">HTML</span>"), html);
    assert.ok(html.includes(">Code</span>"), html);
    assert.ok(html.includes('aria-label="Enable word wrap"'), html);
    assert.ok(html.includes('aria-pressed="false"'), html);
    assert.ok(html.includes('aria-label="Copy code"'), html);
  });

  void it("names a fence language as Codex does", () => {
    const html = render("```ts\nx\n```\n\n```zsh\nls\n```\n\n```text\nhi\n```\n\n```odd\nx\n```");
    assert.ok(html.includes(">TypeScript</span>"), html);
    assert.ok(html.includes(">Bash</span>"), html);
    // Shows the key itself until the catalog has shared.agent.plain_text.
    assert.ok(/>(Plain text|shared\.agent\.plain_text)<\/span>/.test(html), html);
    assert.ok(html.includes(">odd</span>"), html);
  });

  void it("hides the copy button of a fence that is still streaming", () => {
    assert.equal(count(render("```py\nx = 1", undefined, true), 'aria-label="Copy code"'), 0);
    const both = render("```py\na\n```\n\n```py\nb", undefined, true);
    assert.equal(count(both, 'aria-label="Copy code"'), 1, both);
    assert.equal(count(render("```py\nx = 1\n```", undefined, true), 'aria-label="Copy code"'), 1);
    assert.equal(count(render("```py\nx = 1"), 'aria-label="Copy code"'), 1);
  });

  void it("sizes table columns by their longest cell, as Codex does", () => {
    const long = "x".repeat(120);
    const html = render(`| n | text |\n|-|-|\n| 12 | ${long} |`);
    assert.ok(html.includes('<th data-col-size="sm">n</th>'), html);
    assert.ok(html.includes('<td data-col-size="sm" data-numeric="">12</td>'), html);
    assert.ok(html.includes('<td data-col-size="lg">'), html);
  });

  void it("styles code with the Settings code font and size variables", () => {
    const html = render("`a`");
    assert.ok(html.includes("font-family: var(--md-code-font, var(--font-mono))"), html);
    assert.ok(html.includes("font-size: var(--md-code-size, 0.875rem)"), html);
  });

  void it("loads only https and inline raster images", () => {
    const html = render("![ok](https://a.b/i.png) ![bad](http://a.b/i.png)");
    assert.ok(html.includes('<img src="https://a.b/i.png"'), html);
    assert.ok(html.includes("<span>bad</span>"), html);
  });

  void it("shows raw HTML as text but turns <br> into a line break", () => {
    const html = render("| a |\n|-|\n| 1<br>2 |\n\n<b>raw</b>");
    assert.ok(html.includes("<br/>"), html);
    assert.ok(html.includes("&lt;b&gt;raw&lt;/b&gt;"), html);
    assert.ok(html.includes('aria-label="Copy table"'), html);
  });

  void it("renders a user message's code fence as a plain block", () => {
    const html = render("hi\n\n```js\nx\n```", "user-message");
    assert.ok(html.includes("md-user"), html);
    assert.ok(html.includes("bg-current/10"), html);
    assert.ok(!html.includes("Copy code"), html);
  });
});
