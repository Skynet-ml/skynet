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
import { setClientLocale } from "@/shared/lib/runtime-locale";
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
const {
  DatasetUploadCard,
  datasetAttachedNote,
  libraryPickOverride,
  useSendAfterRender,
} = await import("./DatasetUploadCard");

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
  // The read waits for the test, so the unmount comes before it ends.
  let finishRead: () => void = () => {};
  const slowFile = new File([csv], "qa.csv", { type: "text/csv" });
  const readText = slowFile.text.bind(slowFile);
  slowFile.text = () =>
    new Promise<void>((resolve) => {
      finishRead = resolve;
    }).then(readText);
  await React.act(async () =>
    closedRoot.render(
      <DatasetUploadCard initialFile={slowFile} onConfirm={(d) => closedAttached.push(d)} />,
    ),
  );
  assert.match(closedContainer.textContent ?? "", /Reading the file/, "the read is under way");
  await React.act(async () => closedRoot.unmount());
  finishRead();
  await settle();
  assert.equal(closedAttached.length, 0, "a card closed during the read attaches nothing");
  closedContainer.remove();
});

void test("the attached-file note has no RTL isolate marks in a Hebrew UI", () => {
  setClientLocale("he");
  try {
    const note = datasetAttachedNote({
      fileName: "cfca.xlsx",
      rows: [{ address: "1 Main St", lat: 1.5 }],
      rowCount: 1,
      columns: ["address", "lat"],
    });
    assert.doesNotMatch(note, /[\u2066-\u2069]/, "the agent copies clean column names");
    assert.match(note, /\["address","lat"\]/, "the columns are a JSON list");
    assert.match(note, /cfca\.xlsx/);
    assert.match(note, /"address":"1 Main St"/);
  } finally {
    setClientLocale("en");
  }
});

void test("a library pick override does not mark the columns as set", () => {
  const override = libraryPickOverride({
    sourceDatasetId: "ds-1",
    name: "qa",
    columns: ["question", "answer"],
    columnRoles: {},
    columnKinds: {},
    rowCount: 2,
  });
  assert.deepEqual(override, {
    source_dataset_id: "ds-1",
    dataset_ready: true,
    dataset_columns: ["question", "answer"],
  });
  assert.equal("columns_configured" in override, false, "a later upload can clear it");
});

void test("the note cuts a long row by code point, so no surrogate pair splits", () => {
  // `{"tt":"` is 7 code units, so a cut at 300 code units splits an emoji.
  const note = datasetAttachedNote({
    fileName: "emoji.csv",
    rows: [{ tt: "\u{1F600}".repeat(400) }],
    rowCount: 1,
    columns: ["tt", "a,b"],
  });
  assert.doesNotMatch(note, /[\uD800-\uDBFF](?![\uDC00-\uDFFF])/, "no lone high surrogate");
  assert.match(note, /\u{1F600}…/u, "the cut ends on a whole emoji");
  assert.match(note, /\["tt","a,b"\]/, "a column name with a comma stays one name");
});

void test("a send queued after a state change reads the state of the next render", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const seen: Array<string | null> = [];
  let attach: () => void = () => {};

  function Panel() {
    const [roles, setRoles] = React.useState<string | null>("roles of the first file");
    // The chat reads the wizard state of the latest render, as agent.send does.
    const rolesRef = React.useRef(roles);
    rolesRef.current = roles;
    const send = React.useCallback(() => {
      seen.push(rolesRef.current);
    }, []);
    const sendAfterRender = useSendAfterRender(send);
    attach = () => {
      setRoles(null);
      sendAfterRender("note");
    };
    return null;
  }

  await React.act(async () => root.render(<Panel />));
  await React.act(async () => attach());
  assert.deepEqual(seen, [null], "the turn carries no roles from the first file");
  await React.act(async () => root.unmount());
  container.remove();
});
