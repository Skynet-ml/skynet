/*
 * node --test cannot load .tsx, so this runs through jiti (edited-files-card.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, it } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { act } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
});
// The classic JSX transform the test loader applies reads a global React.
Object.assign(globalThis, {
  React,
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  IS_REACT_ACT_ENVIRONMENT: true,
});
after(() => dom.window.close());
const { createRoot } = await import("react-dom/client");
const { EditedFilesCard } = await import("./EditedFilesCard");

const edit = (path: string, added: number, removed: number) => ({
  path,
  before: "",
  after: "",
  added,
  removed,
});

void it("one file shows its name and the turn's line counts, with no buttons", () => {
  const html = renderToStaticMarkup(
    <EditedFilesCard edits={[edit("metric.py", 1, 1)]} onView={null} />,
  );
  assert.match(html, /agent\.edited_files\.edited_one|Edited/);
  assert.match(html, />\+1</);
  assert.match(html, />−1</);
  assert.doesNotMatch(html, /<button/);
  assert.doesNotMatch(html, /<li/);
});

void it("more files show one row each with its own counts", () => {
  const html = renderToStaticMarkup(
    <EditedFilesCard
      edits={[edit("metric.py", 1, 0), edit("optimize.toml", 4, 0)]}
      onView={null}
    />,
  );
  assert.equal(html.match(/<li/g)?.length, 2);
  assert.match(html, /metric\.py<\/span>.*?>\+1<.*?>−0</);
  assert.match(html, /optimize\.toml<\/span>.*?>\+4<.*?>−0</);
  assert.doesNotMatch(html, /<button/);
});

void it("with a Changes pane, each file row opens its file and no Undo shows", () => {
  const html = renderToStaticMarkup(
    <EditedFilesCard
      edits={[edit("metric.py", 1, 0), edit("optimize.toml", 4, 0)]}
      onView={() => {}}
    />,
  );
  assert.equal(html.match(/<button/g)?.length, 2);
  assert.doesNotMatch(html, /undo|reapply/i);
});

const many = Array.from({ length: 10 }, (_, i) => edit(`file${i}.py`, i + 1, i % 2));

void it("more files show the turn's totals under the title", () => {
  const html = renderToStaticMarkup(
    <EditedFilesCard
      edits={[edit("metric.py", 1, 1), edit("optimize.toml", 4, 0)]}
      onView={null}
    />,
  );
  assert.match(html, />\+5<.*?>−1<.*?<ul/);
});

void it("past three files, the rest fold under a closed Show more row", () => {
  const html = renderToStaticMarkup(<EditedFilesCard edits={many} onView={null} />);
  assert.equal(html.match(/<li/g)?.length, 10);
  // The first list holds the three shown files; the folded list holds seven.
  assert.match(html, /<ul class="border-t border-border">(?:<li>.*?<\/li>){3}<\/ul>/);
  assert.match(html, /inert=""[^>]*class="[^"]*grid-rows-\[0fr\]/);
  assert.match(
    html,
    /aria-expanded="false"[^>]*>agent\.edited_files\.show_more|aria-expanded="false"[^>]*>Show 7 more files/,
  );
  assert.match(html, /aria-controls="([^"]+)"/);
  const id = html.match(/aria-controls="([^"]+)"/)![1]!;
  assert.match(html, new RegExp(`id="${id}"`));
});

void it("three files or fewer have no Show more row", () => {
  const html = renderToStaticMarkup(<EditedFilesCard edits={many.slice(0, 3)} onView={null} />);
  assert.doesNotMatch(html, /aria-expanded/);
  assert.doesNotMatch(html, /inert/);
});

void it("the Show more row opens and closes the folded files", async () => {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  await act(async () => root.render(<EditedFilesCard edits={many} onView={null} />));
  const toggle = host.querySelector<HTMLButtonElement>("button[aria-expanded]")!;
  const fold = host.querySelector(`[id="${toggle.getAttribute("aria-controls")!}"]`)!;
  assert.equal(fold.hasAttribute("inert"), true);
  await act(async () => toggle.click());
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  assert.equal(fold.hasAttribute("inert"), false);
  assert.match(fold.className, /grid-rows-\[1fr\]/);
  await act(async () => toggle.click());
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(fold.hasAttribute("inert"), true);
  await act(async () => root.unmount());
  host.remove();
});
