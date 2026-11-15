/*
 * Mounts useChatMedia in jsdom with a real toast container and checks Undo at
 * the file limit. use-chat-media.render.test.ts runs this file through jiti
 * inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import type { ToastTransitionProps } from "react-toastify";
import { buildActiveCatalog } from "@/shared/lib/messages.server";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
Object.assign(globalThis, {
  React,
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  MouseEvent: dom.window.MouseEvent,
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
});
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");
after(() => dom.window.close());

let uploads = 0;
const deleted: string[] = [];
// A test sets this to hold the next upload until it settles the promise.
let nextUpload: Promise<Response> | null = null;
globalThis.fetch = (async (_url: string, init?: RequestInit) => {
  if (init?.method === "DELETE") {
    deleted.push(String(_url).split("/").pop() ?? "");
    return new Response(null, { status: 204 });
  }
  const held = nextUpload;
  nextUpload = null;
  if (held) return held;
  uploads += 1;
  const file = (init?.body as FormData | undefined)?.get("file");
  const name = file instanceof File ? file.name : "f.png";
  const kind = /\.(pdf|docx|xlsx)$/.test(name) ? "document" : "image";
  return Response.json({ id: `s-${uploads}`, name, kind });
}) as typeof fetch;

const { createRoot } = await import("react-dom/client");
const { ToastContainer, toast } = await import("react-toastify");
const { useChatMedia } = await import("./use-chat-media");
const { MAX_MEDIA_PER_MESSAGE, freeChatMediaUrls } = await import("../lib/chat-media");

const errors: unknown[] = [];
toast.error = ((content: unknown) => {
  errors.push(content);
  return "";
}) as typeof toast.error;

/** A transition with no animation, so a closed toast leaves at once in jsdom. */
function Instant({ isIn, done, playToast, children }: ToastTransitionProps) {
  React.useEffect(() => {
    if (isIn) playToast();
    else done();
  }, [isIn, done, playToast]);
  return <>{children}</>;
}

const flush = () => React.act(() => new Promise<void>((resolve) => setTimeout(resolve, 20)));

const image = (n: number) => new File([new Uint8Array(3)], `f-${n}.png`, { type: "image/png" });

void test("Undo at the file limit shows the limit error and adds no extra tile", async () => {
  let latest: ReturnType<typeof useChatMedia> | null = null;
  const keep = (value: ReturnType<typeof useChatMedia>) => {
    latest = value;
  };
  function Harness({ onDraft }: { onDraft: typeof keep }) {
    onDraft(useChatMedia());
    return null;
  }
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () =>
    root.render(
      <>
        <Harness onDraft={keep} />
        <ToastContainer transition={Instant} autoClose={false} />
      </>,
    ),
  );
  const draft = () => latest!;

  await React.act(async () =>
    draft().addFiles(Array.from({ length: MAX_MEDIA_PER_MESSAGE }, (_, n) => image(n))),
  );
  await flush();
  assert.equal(draft().tiles.length, MAX_MEDIA_PER_MESSAGE);
  const removed = draft().ready()![0]!;

  await React.act(async () => draft().remove(draft().tiles[0]!.id));
  await flush();
  await React.act(async () => draft().addFiles([image(99)]));
  await flush();
  assert.equal(draft().tiles.length, MAX_MEDIA_PER_MESSAGE);

  const undo = [...container.querySelectorAll("button")].find((b) => b.textContent === "Undo");
  assert.ok(undo, "the removed-file toast offers Undo");
  await React.act(async () => undo.click());
  await flush();

  assert.equal(draft().tiles.length, MAX_MEDIA_PER_MESSAGE);
  assert.equal(errors.length, 1);
  assert.match(String(errors[0]), /10/);
  assert.deepEqual(deleted, [removed.id]);
  await React.act(async () => root.unmount());
});

void test("a sent file's local URL is freed on the next chat switch", async () => {
  let latest: ReturnType<typeof useChatMedia> | null = null;
  const keep = (value: ReturnType<typeof useChatMedia>) => {
    latest = value;
  };
  function Harness({ onDraft }: { onDraft: typeof keep }) {
    onDraft(useChatMedia());
    return null;
  }
  const revoked: string[] = [];
  const revoke = URL.revokeObjectURL;
  URL.revokeObjectURL = (url: string) => void revoked.push(url);
  const root = createRoot(document.createElement("div"));
  await React.act(async () => root.render(<Harness onDraft={keep} />));
  const draft = () => latest!;

  await React.act(async () => draft().addFiles([image(1)]));
  await flush();
  const sent = draft()
    .ready()!
    .map((m) => m.url);
  await React.act(async () => draft().clear());
  assert.equal(draft().tiles.length, 0);
  assert.deepEqual(revoked, []);

  freeChatMediaUrls();
  assert.deepEqual(revoked, sent);
  URL.revokeObjectURL = revoke;
  await React.act(async () => root.unmount());
});

void test("an unmount frees the local URLs of files still in the draft", async () => {
  let latest: ReturnType<typeof useChatMedia> | null = null;
  const keep = (value: ReturnType<typeof useChatMedia>) => {
    latest = value;
  };
  function Harness({ onDraft }: { onDraft: typeof keep }) {
    onDraft(useChatMedia());
    return null;
  }
  const revoked: string[] = [];
  const revoke = URL.revokeObjectURL;
  URL.revokeObjectURL = (url: string) => void revoked.push(url);
  const root = createRoot(document.createElement("div"));
  await React.act(async () => root.render(<Harness onDraft={keep} />));

  await React.act(async () => latest!.addFiles([image(1), image(2)]));
  await flush();
  const drafted = latest!.tiles.map((m) => m.url);
  assert.equal(drafted.length, 2);
  assert.deepEqual(revoked, []);

  await React.act(async () => root.unmount());
  assert.deepEqual(revoked, drafted);
  URL.revokeObjectURL = revoke;
});

void test("Send waits with a reason while an upload runs, and a failed upload stays marked", async () => {
  let latest: ReturnType<typeof useChatMedia> | null = null;
  const keep = (value: ReturnType<typeof useChatMedia>) => {
    latest = value;
  };
  function Harness({ onDraft }: { onDraft: typeof keep }) {
    onDraft(useChatMedia());
    return null;
  }
  const root = createRoot(document.createElement("div"));
  await React.act(async () => root.render(<Harness onDraft={keep} />));
  assert.equal(latest!.blocked, null);

  let settle: (res: Response) => void = () => {};
  nextUpload = new Promise<Response>((resolve) => (settle = resolve));
  await React.act(async () => latest!.addFiles([image(1)]));
  assert.equal(latest!.blocked, "Wait for the files to finish uploading.");
  assert.equal(latest!.ready(), null);

  await React.act(async () => settle(new Response(null, { status: 500 })));
  await flush();
  assert.equal(latest!.tiles.length, 1, "the failed file keeps its tile");
  assert.equal(latest!.tiles[0]!.status, "failed");
  assert.equal(latest!.blocked, "Remove the files that failed to upload, then send.");
  await React.act(async () => root.unmount());
});

/** Mount the hook alone and return a getter for its latest value. */
async function mountDraft() {
  let latest: ReturnType<typeof useChatMedia> | null = null;
  function Harness() {
    latest = useChatMedia();
    return null;
  }
  const root = createRoot(document.createElement("div"));
  await React.act(async () => root.render(<Harness />));
  return { draft: () => latest!, unmount: () => React.act(async () => root.unmount()) };
}

const pdf = (name: string) => new File([new Uint8Array(3)], name, { type: "application/pdf" });
const documents = <T extends { kind: string }>(media: readonly T[] | null) =>
  (media ?? []).filter((m) => m.kind === "document");

void test("a failed original upload sends no original for any file with its name", async () => {
  const { draft, unmount } = await mountDraft();
  const before = deleted.length;
  nextUpload = Promise.resolve(new Response(null, { status: 500 }));
  await React.act(async () => draft().addOriginals([pdf("a.pdf"), pdf("a.pdf")]));
  await flush();
  const second = `s-${uploads}`;
  // The second file's original must not pair with the first file's text.
  await React.act(async () => draft().keepOriginals([{ name: "a.pdf" }, { name: "a.pdf" }]));
  assert.deepEqual(documents(draft().ready()), []);
  assert.deepEqual(deleted.slice(before), [second]);
  await unmount();
});

void test("removing one of two same-name files deletes both originals", async () => {
  const { draft, unmount } = await mountDraft();
  const before = deleted.length;
  await React.act(async () => draft().addOriginals([pdf("a.pdf"), pdf("a.pdf")]));
  await flush();
  const ids = documents(draft().ready()).map((m) => m.id);
  assert.equal(ids.length, 2);
  await React.act(async () => draft().keepOriginals([{ name: "a.pdf" }]));
  assert.deepEqual(documents(draft().ready()), []);
  assert.deepEqual(deleted.slice(before).sort(), ids.sort());
  await unmount();
});

void test("Send waits for an original upload, and an unmount deletes one that ends later", async () => {
  const { draft, unmount } = await mountDraft();
  const before = deleted.length;
  let settle: (res: Response) => void = () => {};
  nextUpload = new Promise<Response>((resolve) => (settle = resolve));
  await React.act(async () => draft().addOriginals([pdf("late.pdf")]));
  assert.equal(draft().blocked, "Wait for the files to finish uploading.");
  assert.equal(draft().ready(), null);
  await unmount();
  settle(Response.json({ id: "late-1", name: "late.pdf", kind: "document" }));
  await flush();
  assert.deepEqual(deleted.slice(before), ["late-1"]);
});

void test("an original past the file limit stays out of the message and goes on clear", async () => {
  const { draft, unmount } = await mountDraft();
  await React.act(async () =>
    draft().addFiles(Array.from({ length: MAX_MEDIA_PER_MESSAGE - 1 }, (_, n) => image(n))),
  );
  await React.act(async () => draft().addOriginals([pdf("b.pdf"), pdf("c.pdf")]));
  await flush();
  const media = draft().ready()!;
  assert.equal(media.length, MAX_MEDIA_PER_MESSAGE);
  assert.deepEqual(
    documents(media).map((m) => m.name),
    ["b.pdf"],
  );
  const before = deleted.length;
  const last = `s-${uploads}`;
  await React.act(async () => draft().clear());
  assert.deepEqual(deleted.slice(before), [last]);
  await unmount();
});

void test("two same-name originals that do not both fit stay out together and go on clear", async () => {
  const { draft, unmount } = await mountDraft();
  await React.act(async () =>
    draft().addFiles(Array.from({ length: MAX_MEDIA_PER_MESSAGE - 1 }, (_, n) => image(n))),
  );
  await React.act(async () => draft().addOriginals([pdf("a.pdf"), pdf("a.pdf"), pdf("c.pdf")]));
  await flush();
  assert.deepEqual(
    documents(draft().ready()).map((m) => m.name),
    ["c.pdf"],
  );
  const before = deleted.length;
  await React.act(async () => draft().clear());
  assert.deepEqual(deleted.slice(before), [`s-${uploads - 2}`, `s-${uploads - 1}`]);
  await unmount();
});
