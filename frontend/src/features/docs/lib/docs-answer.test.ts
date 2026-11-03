/** The docs answer panel's history, stream parsing, and docs-only links. */

import assert from "node:assert/strict";
import test from "node:test";

import { createElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  askBody,
  isDocsHref,
  isQuestionTooLong,
  keepDocsLinks,
  MAX_QUESTION_CHARS,
  parseStoredHistory,
  toTranscript,
  trimHistory,
} from "./docs-answer.ts";

test("keepDocsLinks keeps links to docs pages", () => {
  const text = "See [Engines](/docs/engines) and [Hebrew](/docs/he/quickstart#run).";
  assert.equal(keepDocsLinks(text), text);
});

test("keepDocsLinks turns other links into plain text", () => {
  assert.equal(
    keepDocsLinks("Go to [the app](https://example.com/x) or [search](/docs-search)."),
    "Go to the app or search.",
  );
  assert.equal(keepDocsLinks("![logo](https://example.com/a.png)"), "logo");
  assert.equal(keepDocsLinks("Visit <https://example.com>."), "Visit `https://example.com`.");
  assert.equal(keepDocsLinks("Visit https://example.com now"), "Visit `https://example.com` now");
});

test("keepDocsLinks drops reference definitions to other sites, with or without a space", () => {
  const out = keepDocsLinks("See [a][r] and [b][s].\n\n[r]:https://evil.example/x\n[s]: <https://evil.example/y>");
  assert.ok(!out.includes("evil.example"));
  const docs = "See [a][d].\n\n[d]:/docs/billing";
  assert.equal(keepDocsLinks(docs), docs);
});

test("keepDocsLinks leaves fenced code alone", () => {
  const text = "```bash\ncurl https://api.example.com\n```\nThen https://x.io";
  assert.equal(keepDocsLinks(text), "```bash\ncurl https://api.example.com\n```\nThen `https://x.io`");
});

test("history keeps the newest non-empty messages", () => {
  const messages = [
    { role: "user" as const, content: "a" },
    { role: "assistant" as const, content: "" },
    { role: "user" as const, content: "b" },
    { role: "assistant" as const, content: "c" },
  ];
  assert.deepEqual(trimHistory(messages, 2), [
    { role: "user", content: "b" },
    { role: "assistant", content: "c" },
  ]);
});

test("stored history ignores malformed data", () => {
  assert.deepEqual(parseStoredHistory(null), []);
  assert.deepEqual(parseStoredHistory("{"), []);
  assert.deepEqual(parseStoredHistory('{"role":"user"}'), []);
  assert.deepEqual(
    parseStoredHistory('[{"role":"user","content":"q"},{"role":"system","content":"x"},7]'),
    [{ role: "user", content: "q" }],
  );
});

test("stored history keeps times, closed thinking and finished tool calls", () => {
  const stored = JSON.stringify([
    { role: "user", content: "q", createdAt: 1000 },
    {
      role: "assistant",
      content: "a",
      createdAt: 2000,
      thinkingSegments: [
        { reasoning: "think", startedAt: 1100, endedAt: null, toolsBefore: 1 },
        { reasoning: "", startedAt: 1200, endedAt: 1300, toolsBefore: 1 },
        "bad",
      ],
      toolCalls: [
        { id: "t1", tool: "search_docs", reason: "q", status: "done", startedAt: 1000, endedAt: 1050, payload: { result: { result_count: 2 } } },
        { id: "t2", tool: "search_docs", reason: "q2", status: "running", startedAt: 1400, endedAt: null },
        { id: 3, tool: "search_docs" },
      ],
    },
  ]);
  assert.deepEqual(parseStoredHistory(stored), [
    { role: "user", content: "q", createdAt: 1000 },
    {
      role: "assistant",
      content: "a",
      createdAt: 2000,
      thinkingSegments: [{ reasoning: "think", startedAt: 1100, endedAt: 1100, toolsBefore: 1 }],
      toolCalls: [
        { id: "t1", tool: "search_docs", reason: "q", status: "done", startedAt: 1000, endedAt: 1050, payload: { result: { result_count: 2 } } },
        { id: "t2", tool: "search_docs", reason: "q2", status: "error", startedAt: 1400, endedAt: 1400 },
      ],
    },
  ]);
});

test("askBody sends only the role and text of earlier turns", () => {
  const earlier = [
    { role: "user" as const, content: "q", createdAt: 1 },
    { role: "assistant" as const, content: "a", createdAt: 2, toolCalls: [] },
  ];
  assert.deepEqual(askBody("next", earlier, "en").history, [
    { role: "user", content: "q" },
    { role: "assistant", content: "a" },
  ]);
});

test("askBody never cuts the question, and keeps the newest history", () => {
  const long = "x".repeat(MAX_QUESTION_CHARS);
  const earlier = Array.from({ length: 10 }, (_, i) => ({
    role: (i % 2 ? "assistant" : "user") as "user" | "assistant",
    content: i === 8 ? long : `m${i}`,
  }));
  const body = askBody(long, earlier, "he");
  assert.equal(body.question, long);
  assert.equal(body.history.length, 6);
  assert.equal(body.history[0].content, "m4");
  assert.equal(body.history[4].content, long);
  assert.equal(body.locale, "he");
});

test("isQuestionTooLong allows the limit and refuses one character more", () => {
  assert.equal(MAX_QUESTION_CHARS, 8000);
  assert.equal(isQuestionTooLong("x".repeat(8000)), false);
  assert.equal(isQuestionTooLong(` ${"x".repeat(8000)} `), false);
  assert.equal(isQuestionTooLong("x".repeat(8001)), true);
});

/** Render an answer the way the docs panel does: filtered text, GFM, and only docs links kept. */
function renderAnswer(markdown: string): string {
  return renderToStaticMarkup(
    createElement(ReactMarkdown, {
      remarkPlugins: [remarkGfm],
      components: {
        a: ({ href, children }: { href?: string; children?: ReactNode }) =>
          href !== undefined && isDocsHref(href) ? createElement("a", { href }, children) : children,
      },
      children: keepDocsLinks(markdown),
    }),
  );
}

test("the docs panel renders no link outside /docs", () => {
  const inputs = [
    "See [here][r].\n\n> [r]:https://evil.example/q",
    "See [here][r].\n\n- [r]:https://evil.example/l",
    "Go to www.evil.example now.",
    "Write to bad@evil.example today.",
  ];
  for (const input of inputs) {
    const html = renderAnswer(input);
    assert.ok(!/<a\b/.test(html), `${input} rendered a link: ${html}`);
  }
});

test("the docs panel keeps /docs links", () => {
  const html = renderAnswer("Read [billing](/docs/billing) and [the docs](/docs).");
  assert.match(html, /<a href="\/docs\/billing">billing<\/a>/);
  assert.match(html, /<a href="\/docs">the docs<\/a>/);
});

test("isDocsHref accepts docs paths only", () => {
  for (const href of ["/docs", "/docs/billing", "/docs#top", "/docs/he/start"]) assert.ok(isDocsHref(href), href);
  for (const href of ["/docs-md", "//evil.example/docs", "http://www.evil.example", "mailto:bad@evil.example", "https://evil.example/q"])
    assert.ok(!isDocsHref(href), href);
});

test("toTranscript keeps docs links only in answers and leaves questions as typed", () => {
  const messages = [
    { role: "user" as const, content: "See [x](https://example.com)" },
    { role: "assistant" as const, content: "Read [Runs](/docs/runs) or [x](https://example.com)." },
  ];
  assert.deepEqual(toTranscript(messages), [
    { role: "user", content: "See [x](https://example.com)" },
    { role: "assistant", content: "Read [Runs](/docs/runs) or x." },
  ]);
  assert.equal(messages[1]?.content, "Read [Runs](/docs/runs) or [x](https://example.com).");
});
