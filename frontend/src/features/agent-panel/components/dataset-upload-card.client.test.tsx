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
  CustomEvent: dom.window.CustomEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const {
  DatasetUploadCard,
  applyDatasetUpload,
  datasetAttachedNote,
  libraryPickOverride,
  useDatasetDraft,
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

void test("the card hands back the picked file, so the chat can keep its original", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const picked: File[] = [];
  const file = new File(["question,answer\nWhat is 2+2?,4\n"], "qa.csv", { type: "text/csv" });

  await React.act(async () =>
    root.render(<DatasetUploadCard initialFile={file} onConfirm={(_, f) => picked.push(f)} />),
  );
  for (let i = 0; i < 20 && picked.length === 0; i += 1) {
    await React.act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
  }

  assert.equal(picked.length, 1);
  assert.equal(picked[0], file, "the same File object, so its bytes can be stored");

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

void test("two sends queued in one tick both go out, in order", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const sent: string[] = [];
  let queueTwo: () => void = () => {};

  function Panel() {
    const send = React.useCallback((note: string) => {
      sent.push(note);
    }, []);
    const sendAfterRender = useSendAfterRender(send);
    queueTwo = () => {
      sendAfterRender("first");
      sendAfterRender("second");
    };
    return null;
  }

  await React.act(async () => root.render(<Panel />));
  await React.act(async () => queueTwo());
  assert.deepEqual(sent, ["first", "second"]);
  await React.act(async () => root.unmount());
  container.remove();
});

void test("a send queued after the render but before the send effect still goes out", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const sent: string[] = [];
  let start: () => void = () => {};

  function Panel() {
    const [late, setLate] = React.useState(false);
    const send = React.useCallback((note: string) => {
      sent.push(note);
    }, []);
    const sendAfterRender = useSendAfterRender(send);
    // A layout effect runs after the commit and before the passive send effect.
    React.useLayoutEffect(() => {
      if (!late) return;
      setLate(false);
      sendAfterRender("late");
    }, [late, sendAfterRender]);
    start = () => {
      setLate(true);
      sendAfterRender("early");
    };
    return null;
  }

  await React.act(async () => root.render(<Panel />));
  await React.act(async () => start());
  assert.deepEqual(sent, ["early", "late"]);
  await React.act(async () => root.unmount());
  container.remove();
});

void test("a disabled card attaches nothing", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const attached: ConfirmedDataset[] = [];
  const file = new File(["question,answer\nWhat is 2+2?,4\n"], "qa.csv", { type: "text/csv" });

  await React.act(async () =>
    root.render(
      <DatasetUploadCard disabled initialFile={file} onConfirm={(d) => attached.push(d)} />,
    ),
  );
  for (let i = 0; i < 10; i += 1) {
    await React.act(async () => new Promise((resolve) => setTimeout(resolve, 10)));
  }

  assert.equal(attached.length, 0);
  assert.doesNotMatch(container.textContent ?? "", /File attached/);
  await React.act(async () => root.unmount());
  container.remove();
});

void test("an upload clears the earlier roles and hands off no roles", () => {
  const calls: Array<[string, string, unknown?]> = [];
  const wizard = {
    setField: (key: string, value: unknown) => calls.push(["set", key, value]),
    clearField: (key: string) => calls.push(["clear", key]),
  };
  const dataset: ConfirmedDataset = {
    fileName: "qa.csv",
    rows: [{ question: "What is 2+2?", answer: "4" }],
    rowCount: 1,
    columns: ["question", "answer"],
  };

  const { detail, override } = applyDatasetUpload(wizard, dataset, "staged-1");

  assert.deepEqual(calls, [
    ["set", "staged_dataset_id", "staged-1"],
    ["set", "dataset_columns", ["question", "answer"]],
    ["set", "dataset_ready", true],
    ["clear", "input_fields"],
    ["clear", "expected_field"],
    ["clear", "columns_configured"],
  ]);
  assert.deepEqual(detail, {
    dataset: dataset.rows,
    dataset_filename: "qa.csv",
    staged_dataset_id: "staged-1",
    wizard_state: { dataset_ready: true, dataset_columns: ["question", "answer"] },
  });
  assert.deepEqual(override, {
    staged_dataset_id: "staged-1",
    dataset_ready: true,
    dataset_columns: ["question", "answer"],
  });

  calls.length = 0;
  const unstaged = applyDatasetUpload(wizard, dataset, undefined);
  assert.equal(unstaged.override, undefined, "no staged id, no override");
  assert.deepEqual(calls, [
    ["clear", "input_fields"],
    ["clear", "expected_field"],
    ["clear", "columns_configured"],
  ]);
});

/** Mount useDatasetDraft with a draft in state and a recording wizard (SKY-780). */
async function mountDatasetDraft(stage: () => Promise<string | undefined>) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const calls: Array<[string, string, unknown?]> = [];
  const released: string[] = [];
  const wizard = {
    setField: (key: string, value: unknown) => calls.push(["set", key, value]),
    clearField: (key: string) => calls.push(["clear", key]),
  };
  const live: {
    draft: string;
    setDraft: (d: string) => void;
    api: ReturnType<typeof useDatasetDraft>;
  } = {} as never;
  function Panel() {
    const [draft, setDraft] = React.useState("my text");
    live.draft = draft;
    live.setDraft = setDraft;
    live.api = useDatasetDraft({
      draft,
      setDraft,
      stage,
      wizard,
      onRelease: (owner) => released.push(owner),
    });
    return null;
  }
  await React.act(async () => root.render(<Panel />));
  const unmount = async () => {
    await React.act(async () => root.unmount());
    container.remove();
  };
  return { live, calls, released, unmount };
}

const QA_DATASET: ConfirmedDataset = {
  fileName: "qa.csv",
  rows: [{ q: "2+2", a: "4" }],
  rowCount: 1,
  columns: ["q", "a"],
};

void test("a confirmed dataset waits in the draft as a card and sends nothing", async () => {
  const { live, calls, unmount } = await mountDatasetDraft(async () => "staged-1");
  const staged: unknown[] = [];
  const onStaged = (e: Event) => staged.push((e as CustomEvent).detail);
  window.addEventListener("wizard:dataset-staged", onStaged);
  await React.act(async () => live.api.attach(QA_DATASET));
  assert.match(
    live.draft,
    /^my text\n\n<attached_file name="qa.csv" id="[0-9a-f-]{36}">\nDataset `qa.csv` attached \(1 rows\)/,
  );
  assert.equal(calls.length, 0, "the wizard changes only on Send");
  assert.ok(!live.api.staging(live.draft), "staging ended");
  assert.ok(live.api.holds(live.draft), "the draft holds the card");
  const taken = live.api.take(live.draft);
  assert.deepEqual(taken, {
    override: { staged_dataset_id: "staged-1", dataset_ready: true, dataset_columns: ["q", "a"] },
  });
  assert.ok(calls.some(([op, key]) => op === "set" && key === "staged_dataset_id"));
  assert.ok(calls.some(([op, key]) => op === "clear" && key === "input_fields"));
  assert.equal(staged.length, 1, "Send hands the dataset to the wizard");
  window.removeEventListener("wizard:dataset-staged", onStaged);
  assert.equal(live.api.take(live.draft), null, "a dataset applies once");
  assert.ok(!live.api.holds(live.draft), "a sent card is no longer pending");
  await unmount();
});

const PASTED = '<attached_file name="qa.csv">\nmy own notes\n</attached_file>';

void test("attach gives the new card a fixed id, and a new card takes out only the pending one (SKY-782)", async () => {
  const { live, unmount } = await mountDatasetDraft(async () => "staged-l");
  await React.act(async () => live.setDraft(`my text\n\n${PASTED}`));
  let first = "";
  await React.act(async () => void (first = live.api.attach(QA_DATASET)));
  assert.match(first, /^[0-9a-f-]{36}$/);
  assert.ok(live.draft.includes(`id="${first}"`), "the card carries its id");
  assert.ok(live.draft.includes(PASTED), "a file with the same name and other text stays");
  let second = "";
  await React.act(async () => void (second = live.api.attach({ ...QA_DATASET, rowCount: 2 })));
  assert.notEqual(second, first);
  assert.ok(!live.draft.includes(first), "the earlier pending card left");
  assert.ok(live.draft.includes(`id="${second}"`));
  assert.ok(live.draft.includes(PASTED));
  await unmount();
});

void test("Send waits while the rows stage", async () => {
  let finish: (id: string) => void = () => {};
  const { live, unmount } = await mountDatasetDraft(
    () => new Promise<string>((resolve) => (finish = resolve)),
  );
  await React.act(async () => live.api.attach(QA_DATASET));
  assert.ok(live.api.staging(live.draft));
  await React.act(async () => finish("staged-2"));
  assert.ok(!live.api.staging(live.draft));
  await unmount();
});

void test("a removed card changes nothing on Send", async () => {
  const { live, calls, unmount } = await mountDatasetDraft(async () => "staged-3");
  await React.act(async () => live.api.attach(QA_DATASET));
  await React.act(async () => live.setDraft("my text"));
  assert.ok(!live.api.holds(live.draft));
  assert.ok(!live.api.staging(live.draft));
  assert.equal(live.api.take(live.draft), null);
  assert.equal(calls.length, 0);
  await unmount();
});

void test("a library pick waits in the draft and applies its override on Send", async () => {
  const { live, calls, unmount } = await mountDatasetDraft(async () => undefined);
  await React.act(async () =>
    live.api.attachLibrary({
      sourceDatasetId: "lib-1",
      name: "Saved QA",
      rowCount: 5,
      columns: ["q", "a"],
      columnRoles: { q: "input", a: "output" },
    } as never),
  );
  assert.match(live.draft, /<attached_file name="Saved QA" id="[0-9a-f-]{36}">/);
  assert.equal(calls.length, 0);
  const taken = live.api.take(live.draft);
  assert.deepEqual(taken?.override, {
    source_dataset_id: "lib-1",
    dataset_ready: true,
    dataset_columns: ["q", "a"],
  });
  assert.ok(calls.some(([op, key]) => op === "set" && key === "source_dataset_id"));
  await unmount();
});

void test("a removed card drops its dataset and reopens its tool card", async () => {
  const { live, released, unmount } = await mountDatasetDraft(async () => "staged-4");
  await React.act(async () => live.api.attach(QA_DATASET, "call-1"));
  assert.equal(released.length, 0);
  await React.act(async () => live.setDraft("my text"));
  assert.deepEqual(released, ["call-1"]);
  assert.ok(!live.api.holds(live.draft));
  await unmount();
});

void test("a second dataset reopens the first tool card, and Send reopens nothing", async () => {
  const { live, released, unmount } = await mountDatasetDraft(async () => "staged-5");
  await React.act(async () => live.api.attach(QA_DATASET, "call-1"));
  await React.act(async () => live.api.attach({ ...QA_DATASET, fileName: "b.csv" }, "call-2"));
  assert.deepEqual(released, ["call-1"]);
  assert.ok(live.api.take(live.draft));
  await React.act(async () => live.setDraft(""));
  assert.deepEqual(released, ["call-1"], "a sent dataset keeps its tool card confirmed");
  await unmount();
});
