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
// A test sets this to hold every restore read until it settles the promise.
let heldReads: Promise<void> | null = null;
globalThis.fetch = (async (_url: string, init?: RequestInit) => {
  if (init?.method === "DELETE") {
    deleted.push(String(_url).split("/").pop() ?? "");
    return new Response(null, { status: 204 });
  }
  // A restore reads a stored file's bytes; "gone" is a file the server no longer has.
  if (!init?.method) {
    if (heldReads) await heldReads;
    if (String(_url).endsWith("/gone")) return new Response(null, { status: 404 });
    return new Response(new Uint8Array(3), { headers: { "content-type": "image/png" } });
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
  assert.equal(latest!.blocked, "Remove the files that failed to attach, then send.");
  await React.act(async () => root.unmount());
});

void test("a restore uploads sent files again as new draft files, and a remove deletes them", async () => {
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
  deleted.length = 0;
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
  const sent = [
    { id: "sent-1", name: "a.png", kind: "image" as const, url: "blob:old-1" },
    { id: "sent-2", name: "b.png", kind: "image" as const, url: "blob:old-2" },
  ];

  // Send waits from the restore call, before any tile shows.
  let hold!: (r: Response) => void;
  nextUpload = new Promise<Response>((resolve) => (hold = resolve));
  await React.act(async () => latest!.restore(sent));
  assert.equal(latest!.blocked, "Wait for the files to finish uploading.");
  await flush();
  assert.equal(latest!.tiles.length, 2);
  assert.equal(latest!.ready(), null, "the first upload still runs");
  assert.equal(latest!.blocked, "Wait for the files to finish uploading.");
  hold(Response.json({ id: "new-1", name: "a.png", kind: "image" }));
  await flush();
  const ids = latest!.ready()?.map((m) => m.id) ?? [];
  assert.equal(ids.length, 2);
  assert.ok(!ids.includes("sent-1") && !ids.includes("sent-2"), "the draft gets new ids");
  assert.equal(latest!.blocked, null);
  assert.ok(latest!.tiles[0]!.url.startsWith("blob:"));

  // A chat switch frees the chat's display URLs, not the draft's own.
  freeChatMediaUrls();
  assert.deepEqual(revoked, []);

  // A second restore into a draft that has files changes nothing.
  await React.act(async () => latest!.restore([sent[0]!]));
  await flush();
  assert.equal(latest!.tiles.length, 2);

  const url = latest!.tiles[0]!.url;
  await React.act(async () => latest!.remove(latest!.tiles[0]!.id));
  await React.act(async () => toast.dismiss());
  await flush();
  assert.equal(latest!.tiles.length, 1);
  assert.deepEqual(deleted, ["new-1"], "the draft's own copy goes");
  assert.deepEqual(revoked, [url]);
  URL.revokeObjectURL = revoke;
  await React.act(async () => root.unmount());
});

void test("a restored file that does not load shows as failed and blocks Send", async () => {
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

  await React.act(async () =>
    latest!.restore([{ id: "gone", name: "a.png", kind: "image", url: "" }]),
  );
  await flush();
  assert.equal(latest!.tiles.length, 1);
  assert.equal(latest!.tiles[0]!.status, "failed");
  assert.equal(latest!.blocked, "Remove the files that failed to attach, then send.");
  await React.act(async () => root.unmount());
});

void test("a restore drops its files when a newer restore starts or the user adds files", async () => {
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
  const sent = (id: string) => [{ id, name: `${id}.png`, kind: "image" as const, url: "" }];
  let release!: () => void;

  // The user adds a file while the restore loads: only the user's file stays.
  heldReads = new Promise<void>((resolve) => (release = resolve));
  await React.act(async () => latest!.restore(sent("old")));
  await React.act(async () => latest!.addFiles([image(1)]));
  await flush();
  const before = uploads;
  heldReads = null;
  release();
  await flush();
  assert.equal(latest!.tiles.length, 1);
  assert.equal(latest!.tiles[0]!.name, "f-1.png");
  assert.equal(uploads, before, "the dropped restore uploads nothing");
  await React.act(async () => latest!.clear());

  // A newer restore starts while the first loads: only the newer one fills the draft.
  heldReads = new Promise<void>((resolve) => (release = resolve));
  await React.act(async () => latest!.restore(sent("first")));
  await React.act(async () => latest!.restore([...sent("second"), ...sent("third")]));
  heldReads = null;
  release();
  await flush();
  assert.deepEqual(
    latest!.tiles.map((t) => t.name),
    ["second.png", "third.png"],
  );
  assert.equal(latest!.blocked, null);
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

void test("a restore sends a document again as an original, not a tile", async () => {
  const { draft, unmount } = await mountDraft();
  const errorsBefore = errors.length;
  await React.act(async () =>
    draft().restore([
      { id: "sent-img", name: "a.png", kind: "image", url: "" },
      { id: "sent-doc", name: "r.pdf", kind: "document", url: "" },
      { id: "gone", name: "lost.pdf", kind: "document", url: "" },
    ]),
  );
  await flush();
  assert.deepEqual(
    draft().tiles.map((t) => t.name),
    ["a.png"],
  );
  // A document that does not load stays out with no error: its file still goes as text.
  assert.equal(errors.length, errorsBefore);
  await React.act(async () => draft().keepOriginals([{ name: "r.pdf" }]));
  assert.deepEqual(
    documents(draft().ready()).map((m) => m.name),
    ["r.pdf"],
  );
  await unmount();
});
