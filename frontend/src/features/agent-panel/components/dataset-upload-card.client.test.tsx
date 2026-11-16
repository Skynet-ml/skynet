/*
 * Mounts DatasetUploadCard in jsdom and checks that a read file attaches
 * with no column-role step (SKY-742).
 * dataset-upload-card.render.test.ts runs this file through jiti inside
 * `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { ConfirmedDataset } from "./DatasetUploadCard";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
Object.assign(globalThis, {
  React,
  window: dom.window,
  self: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { DatasetUploadCard } = await import("./DatasetUploadCard");

after(() => dom.window.close());

void test("a read file attaches at once, with no role picker and no confirm button", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const attached: ConfirmedDataset[] = [];
  const file = new File(["question,answer\nWhat is 2+2?,4\nCapital of France?,Paris\n"], "qa.csv", {
    type: "text/csv",
  });

  await React.act(async () =>
    root.render(<DatasetUploadCard initialFile={file} onConfirm={(d) => attached.push(d)} />),
  );
  for (let i = 0; i < 20 && attached.length === 0; i += 1) {
    await React.act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
  }

  assert.equal(attached.length, 1, "the card attaches the file once");
  const [dataset] = attached;
  assert.equal(dataset?.fileName, "qa.csv");
  assert.equal(dataset?.rowCount, 2);
  assert.deepEqual(dataset?.columns, ["question", "answer"]);
  assert.equal("columnRoles" in (dataset ?? {}), false, "the upload carries no column roles");
  const text = container.textContent ?? "";
  assert.match(text, /File attached/);
  assert.doesNotMatch(text, /Confirm columns|Choose a role/);
  assert.equal(container.querySelectorAll("button").length, 0, "no role or confirm controls");

  await React.act(async () => root.unmount());
  container.remove();
});

void test("a Strict Mode remount attaches once, and a closed card attaches nothing", async () => {
  const csv = "question,answer\nWhat is 2+2?,4\n";
  const settle = async () => {
    for (let i = 0; i < 10; i += 1) {
      await React.act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
    }
  };

  const strictContainer = document.createElement("div");
  document.body.append(strictContainer);
  const strictRoot = createRoot(strictContainer);
  const strictAttached: ConfirmedDataset[] = [];
  await React.act(async () =>
    strictRoot.render(
      <React.StrictMode>
        <DatasetUploadCard
          initialFile={new File([csv], "qa.csv", { type: "text/csv" })}
          onConfirm={(d) => strictAttached.push(d)}
        />
      </React.StrictMode>,
    ),
  );
  await settle();
  assert.equal(
    strictAttached.length,
    1,
    "Strict Mode runs the read effect twice but attaches once",
  );
  await React.act(async () => strictRoot.unmount());
  strictContainer.remove();

  const closedContainer = document.createElement("div");
  document.body.append(closedContainer);
  const closedRoot = createRoot(closedContainer);
  const closedAttached: ConfirmedDataset[] = [];
  await React.act(async () => {
    closedRoot.render(
      <DatasetUploadCard
        initialFile={new File([csv], "qa.csv", { type: "text/csv" })}
        onConfirm={(d) => closedAttached.push(d)}
      />,
    );
    closedRoot.unmount();
  });
  await settle();
  assert.equal(closedAttached.length, 0, "a card closed during the read attaches nothing");
  closedContainer.remove();
});
