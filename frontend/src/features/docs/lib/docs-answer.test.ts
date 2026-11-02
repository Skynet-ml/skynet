/** The docs answer panel's history, stream parsing, and docs-only links. */

import assert from "node:assert/strict";
import test from "node:test";

import {
  askBody,
  chunkText,
  keepDocsLinks,
  parseStoredHistory,
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

test("chunkText reads OpenAI-style deltas and ignores everything else", () => {
  assert.equal(chunkText({ choices: [{ delta: { content: "Hi" } }] }), "Hi");
  assert.equal(chunkText({ choices: [{ delta: {} }] }), "");
  assert.equal(chunkText({ error: { message: "x" } }), "");
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

test("askBody cuts the question and history to the backend limits", () => {
  const long = "x".repeat(5000);
  const earlier = Array.from({ length: 10 }, (_, i) => ({
    role: (i % 2 ? "assistant" : "user") as "user" | "assistant",
    content: i === 9 ? long : `m${i}`,
  }));
  const body = askBody(long, earlier, "he");
  assert.equal(body.question.length, 1000);
  assert.equal(body.history.length, 6);
  assert.equal(body.history[0].content, "m4");
  assert.equal(body.history[5].content.length, 4000);
  assert.equal(body.locale, "he");
});
