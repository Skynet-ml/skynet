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
const originalUploads: string[] = [];
// The name field of each original upload, which the server types it by.
const originalNames: unknown[] = [];
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
  const form = init?.body as FormData | undefined;
  const file = form?.get("file");
  const name = file instanceof File ? file.name : "f.png";
  // The server stores an upload marked as an original as a document, whatever its type.
  const original = form?.get("original") === "true";
  if (original) originalUploads.push(name);
  if (original) originalNames.push(form?.get("name"));
  const kind = original || /\.(pdf|docx|xlsx)$/.test(name) ? "document" : "image";
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
  await React.act(async () => void draft().addOriginals([pdf("a.pdf"), pdf("a.pdf")]));
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
  await React.act(async () => void draft().addOriginals([pdf("a.pdf"), pdf("a.pdf")]));
  await flush();
  const ids = documents(draft().ready()).map((m) => m.id);
  assert.equal(ids.length, 2);
  await React.act(async () => draft().keepOriginals([{ name: "a.pdf" }]));
  assert.deepEqual(documents(draft().ready()), []);
  assert.deepEqual(deleted.slice(before).sort(), ids.sort());
  await unmount();
});

void test("a file of any type keeps its original, which goes out on Send (SKY-782)", async () => {
  const { draft, unmount } = await mountDraft();
  const before = originalUploads.length;
  const csv = new File(["a,b\n1,2\n"], "rows.csv", { type: "text/csv" });
  await React.act(async () => void draft().addOriginals([csv, pdf('x.p"df')]));
  await flush();
  assert.deepEqual(originalUploads.slice(before), ["rows.csv", 'x.p"df']);
  // The browser escapes '"' in the multipart filename; the field keeps it.
  assert.deepEqual(originalNames.slice(before), ["rows.csv", 'x.p"df']);
  assert.deepEqual(
    documents(draft().ready()).map((m) => m.name),
    ["rows.csv", 'x.p"df'],
  );
  await unmount();
});

void test("an original over the file limit is not uploaded; the file keeps only its text", async () => {
  const { draft, unmount } = await mountDraft();
  const before = originalUploads.length;
  const big = new File(["x"], "big.csv", { type: "text/csv" });
  Object.defineProperty(big, "size", { value: 20 * 1024 * 1024 + 1 });
  let ids: string[] = [];
  await React.act(async () => void (ids = draft().addOriginals([big, pdf("a.pdf")])));
  await flush();
  assert.equal(ids.length, 1);
  assert.deepEqual(originalUploads.slice(before), ["a.pdf"]);
  assert.deepEqual(
    documents(draft().ready()).map((m) => m.name),
    ["a.pdf"],
  );
  await unmount();
});

void test("a replacing original drops only as many restored same-key originals as it is told", async () => {
  const { draft, unmount } = await mountDraft();
  const before = deleted.length;
  let own: string[] = [];
  // A composer file's original comes first, so a drop of the oldest would take it.
  await React.act(async () => void (own = draft().addOriginals([pdf("a_1_.pdf")])));
  await React.act(async () =>
    draft().restore([
      { id: "sent-a1", name: "a<1>.pdf", kind: "document", url: "" },
      { id: "sent-a2", name: "a<1>.pdf", kind: "document", url: "" },
      { id: "sent-b", name: "b.pdf", kind: "document", url: "" },
    ]),
  );
  await flush();
  const stored = documents(draft().ready()).map((m) => m.id);
  assert.equal(stored.length, 4);
  let next: string[] = [];
  // The block shows "a<1>.pdf" as "a_1_.pdf"; both have one key.
  await React.act(
    async () => void (next = draft().addOriginals([pdf("a_1_.pdf")], { replace: 1 })),
  );
  await flush();
  const files = [
    { name: "a_1_.pdf" },
    { name: "a_1_.pdf" },
    { name: "a_1_.pdf" },
    { name: "b.pdf" },
  ];
  const shown = draft()
    .shownOriginals(files)
    .map((m) => m.id);
  assert.equal(shown.length, 4);
  assert.ok(shown.includes(own[0]!));
  assert.ok(shown.includes(next[0]!));
  assert.deepEqual(deleted.slice(before), [stored[1]]);
  await unmount();
});

void test("a replacing original skips only that many originals a restore still loads", async () => {
  const { draft, unmount } = await mountDraft();
  let release!: () => void;
  heldReads = new Promise<void>((resolve) => (release = resolve));
  await React.act(async () =>
    draft().restore([
      { id: "sent-a1", name: "a.pdf", kind: "document", url: "" },
      { id: "sent-a2", name: "a.pdf", kind: "document", url: "" },
      { id: "sent-b", name: "b.pdf", kind: "document", url: "" },
    ]),
  );
  let next: string[] = [];
  await React.act(async () => void (next = draft().addOriginals([pdf("a.pdf")], { replace: 1 })));
  heldReads = null;
  release();
  await flush();
  const shown = draft().shownOriginals([{ name: "a.pdf" }, { name: "a.pdf" }, { name: "b.pdf" }]);
  assert.equal(shown.length, 3);
  assert.equal(shown.filter((m) => m.name === "a.pdf").length, 2);
  assert.ok(shown.some((m) => m.id === next[0]));
  await unmount();
});

void test("dropOriginal says whether the draft held the original", async () => {
  const { draft, unmount } = await mountDraft();
  let ids: string[] = [];
  await React.act(async () => void (ids = draft().addOriginals([pdf("a.pdf")])));
  let first = false;
  let second = true;
  await React.act(async () => void (first = draft().dropOriginal(ids[0]!)));
  await React.act(async () => void (second = draft().dropOriginal(ids[0]!)));
  assert.deepEqual([first, second], [true, false]);
  await unmount();
});

void test("dropOriginal takes out only that original, and deletes its stored copy", async () => {
  const { draft, unmount } = await mountDraft();
  const before = deleted.length;
  let first: string[] = [];
  await React.act(async () => void (first = draft().addOriginals([pdf("a.pdf")])));
  await React.act(async () => void draft().addOriginals([pdf("a.pdf")]));
  await flush();
  const [firstStored, secondStored] = documents(draft().ready()).map((m) => m.id);
  await React.act(async () => draft().dropOriginal(first[0]!));
  assert.deepEqual(
    documents(draft().ready()).map((m) => m.id),
    [secondStored],
  );
  assert.deepEqual(deleted.slice(before), [firstStored]);
  // An id that already left the draft does nothing.
  await React.act(async () => draft().dropOriginal(first[0]!));
  assert.deepEqual(deleted.slice(before), [firstStored]);
  await unmount();
});

/** Run ``body`` with URL.revokeObjectURL recording the URLs it frees. */
async function recordingRevokes(body: (revoked: string[]) => Promise<void>) {
  const revoked: string[] = [];
  const revoke = URL.revokeObjectURL;
  URL.revokeObjectURL = (url: string) => void revoked.push(url);
  try {
    await body(revoked);
  } finally {
    URL.revokeObjectURL = revoke;
  }
}

const both = [{ name: "a.pdf" }, { name: "b.pdf" }];

void test("the draft shows its originals from local URLs, and frees each one that leaves (SKY-736)", async () => {
  await recordingRevokes(async (revoked) => {
    const { draft, unmount } = await mountDraft();
    await React.act(async () => void draft().addOriginals([pdf("a.pdf"), pdf("b.pdf")]));
    await flush();
    const [a, b] = draft().shownOriginals(both);
    assert.deepEqual(
      draft()
        .shownOriginals(both)
        .map((o) => [o.name, o.kind]),
      [
        ["a.pdf", "document"],
        ["b.pdf", "document"],
      ],
    );
    assert.ok(a!.url.startsWith("blob:") && b!.url.startsWith("blob:"), "local object URLs");
    await React.act(async () => draft().keepOriginals([{ name: "b.pdf" }]));
    assert.deepEqual(revoked, [a!.url]);
    assert.deepEqual(
      draft()
        .shownOriginals([{ name: "b.pdf" }])
        .map((o) => o.name),
      ["b.pdf"],
    );
    await React.act(async () => draft().clear());
    assert.deepEqual(revoked, [a!.url, b!.url]);
    assert.deepEqual(draft().shownOriginals([{ name: "b.pdf" }]), []);
    await unmount();
  });
});

void test("a failed original upload and an unmount free the draft's local URLs", async () => {
  await recordingRevokes(async (revoked) => {
    const { draft, unmount } = await mountDraft();
    let fail: () => void = () => {};
    nextUpload = new Promise<Response>(
      (resolve) => (fail = () => resolve(new Response(null, { status: 500 }))),
    );
    await React.act(async () => void draft().addOriginals([pdf("a.pdf"), pdf("b.pdf")]));
    const urls = draft()
      .shownOriginals(both)
      .map((o) => o.url);
    assert.equal(urls.length, 2);
    fail();
    await flush();
    // Either upload can take the failed response; its URL goes at once.
    assert.equal(revoked.length, 1, "the failed upload frees its URL");
    assert.ok(urls.includes(revoked[0]!));
    assert.equal(draft().shownOriginals(both).length, 1);
    await unmount();
    assert.deepEqual(revoked.sort(), [...urls].sort(), "the unmount frees the rest");
  });
});

void test("an older commit's effect does not free a URL its draft still shows", async () => {
  let latest: ReturnType<typeof useChatMedia> | null = null;
  // Runs once, in the layout phase of the next commit.
  let onCommit: (() => void) | null = null;
  function Harness() {
    latest = useChatMedia();
    React.useLayoutEffect(() => {
      const run = onCommit;
      onCommit = null;
      run?.();
    });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  await React.act(async () => root.render(<Harness />));
  const fetchAll = globalThis.fetch;
  // The uploads never end, so only keepOriginals takes a file out.
  globalThis.fetch = ((url: string, init?: RequestInit) =>
    init?.method === "POST"
      ? new Promise<Response>(() => {})
      : fetchAll(url, init)) as typeof fetch;
  // At each free, the originals the rendered draft shows.
  const shownAtFree: string[][] = [];
  const revoke = URL.revokeObjectURL;
  URL.revokeObjectURL = () =>
    void shownAtFree.push(latest!.shownOriginals(both).map((o) => o.name));
  try {
    // a's URL is queued in the commit that first shows a and b. That commit's
    // passive effect runs before the render without a, so it must keep a's URL.
    onCommit = () => latest!.keepOriginals([{ name: "b.pdf" }]);
    await React.act(async () => latest!.addOriginals([pdf("a.pdf"), pdf("b.pdf")]));
    assert.deepEqual(shownAtFree, [["b.pdf"]], "a is freed only once the draft without it renders");
  } finally {
    globalThis.fetch = fetchAll;
    URL.revokeObjectURL = revoke;
  }
  await React.act(async () => root.unmount());
});

void test("a failed original's URL is freed only after the draft without it renders", async () => {
  const { draft, unmount } = await mountDraft();
  // What the rendered draft shows at each free.
  const shownAtFree: string[][] = [];
  const revoke = URL.revokeObjectURL;
  URL.revokeObjectURL = () =>
    void shownAtFree.push(
      draft()
        .shownOriginals([{ name: "a.pdf" }])
        .map((o) => o.name),
    );
  try {
    let fail: () => void = () => {};
    nextUpload = new Promise<Response>(
      (resolve) => (fail = () => resolve(new Response(null, { status: 500 }))),
    );
    await React.act(async () => draft().addOriginals([pdf("a.pdf")]));
    assert.deepEqual(
      draft()
        .shownOriginals([{ name: "a.pdf" }])
        .map((o) => o.name),
      ["a.pdf"],
    );
    fail();
    await flush();
    assert.deepEqual(shownAtFree, [[]]);
  } finally {
    URL.revokeObjectURL = revoke;
  }
  await unmount();
});

void test("a kept-out or sent original's URL is freed only after the draft without it renders", async () => {
  const { draft, unmount } = await mountDraft();
  await React.act(async () => void draft().addOriginals([pdf("a.pdf"), pdf("b.pdf")]));
  await flush();
  const urls = new Map(
    draft()
      .shownOriginals(both)
      .map((o) => [o.url, o.name]),
  );
  // At each free of one of these URLs, the names the rendered draft shows.
  const shownAtFree: Array<[string, string[]]> = [];
  const revoke = URL.revokeObjectURL;
  URL.revokeObjectURL = (url: string) => {
    const name = urls.get(url);
    if (name)
      shownAtFree.push([
        name,
        draft()
          .shownOriginals(both)
          .map((o) => o.name),
      ]);
  };
  try {
    await React.act(async () => draft().keepOriginals([{ name: "b.pdf" }]));
    assert.deepEqual(shownAtFree, [["a.pdf", ["b.pdf"]]], "a file that leaves");
    await React.act(async () => draft().clear());
    assert.deepEqual(shownAtFree.slice(1), [["b.pdf", []]], "a sent draft");
  } finally {
    URL.revokeObjectURL = revoke;
  }
  await unmount();
});

void test("an unmount before the draft commits frees each left original's URL once", async () => {
  for (const leave of ["keep", "clear"] as const) {
    let latest: ReturnType<typeof useChatMedia> | null = null;
    function Harness() {
      latest = useChatMedia();
      return null;
    }
    const root = createRoot(document.createElement("div"));
    await React.act(async () => root.render(<Harness />));
    await React.act(async () => latest!.addOriginals([pdf("a.pdf"), pdf("b.pdf")]));
    await flush();
    const urls = latest!.shownOriginals(both).map((o) => o.url);
    await recordingRevokes(async (revoked) => {
      // The unmount comes in the same act, so the draft without the files never commits.
      await React.act(async () => {
        if (leave === "keep") latest!.keepOriginals([{ name: "b.pdf" }]);
        else latest!.clear();
        root.unmount();
      });
      const mine = revoked.filter((url) => urls.includes(url));
      assert.deepEqual(mine.sort(), [...urls].sort(), `${leave}: each URL is freed once`);
    });
  }
});

void test("a draft shows no original for two same-name files when one upload fails", async () => {
  const { draft, unmount } = await mountDraft();
  nextUpload = Promise.resolve(new Response(null, { status: 500 }));
  await React.act(async () => void draft().addOriginals([pdf("a.pdf"), pdf("a.pdf")]));
  await flush();
  // The one left could be either file's, and Send drops it, so no tile shows it.
  assert.deepEqual(draft().shownOriginals([{ name: "a.pdf" }, { name: "a.pdf" }]), []);
  await unmount();
});

void test("a draft shows no original that the file limit keeps out of the message", async () => {
  const { draft, unmount } = await mountDraft();
  await React.act(async () =>
    draft().addFiles(Array.from({ length: MAX_MEDIA_PER_MESSAGE }, (_, n) => image(n))),
  );
  await React.act(async () => draft().addOriginals([pdf("a.pdf")]));
  await flush();
  assert.deepEqual(draft().shownOriginals([{ name: "a.pdf" }]), []);
  await unmount();
});

void test("a draft original keeps one object when its upload ends, so the viewer does not reload", async () => {
  const { draft, unmount } = await mountDraft();
  let settle: (res: Response) => void = () => {};
  nextUpload = new Promise<Response>((resolve) => (settle = resolve));
  await React.act(async () => draft().addOriginals([pdf("a.pdf")]));
  const [before] = draft().shownOriginals([{ name: "a.pdf" }]);
  assert.ok(draft().blocked, "the upload still runs");
  settle(Response.json({ id: "a-1", name: "a.pdf", kind: "document" }));
  await flush();
  assert.equal(draft().blocked, null, "the upload ended");
  assert.equal(draft().shownOriginals([{ name: "a.pdf" }])[0], before);
  await unmount();
});

void test("a draft PDF's local URL holds a PDF blob, whatever type the file claims", async () => {
  const types: string[] = [];
  const create = URL.createObjectURL;
  URL.createObjectURL = (blob: Blob) => {
    types.push(blob.type);
    return create(blob);
  };
  try {
    const { draft, unmount } = await mountDraft();
    const page = new File(["<script>1</script>"], "a.pdf", { type: "text/html" });
    const doc = new File([new Uint8Array(3)], "b.docx", { type: "application/x-test" });
    await React.act(async () => draft().addOriginals([page, doc]));
    await flush();
    assert.deepEqual(types, ["application/pdf", "application/x-test"]);
    await unmount();
  } finally {
    URL.createObjectURL = create;
  }
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

void test("a media file the server stores as a document fails and its copy goes", async () => {
  const { draft, unmount } = await mountDraft();
  const before = deleted.length;
  const errorsBefore = errors.length;
  // An image name and type, but the fake server sees a PDF.
  nextUpload = Promise.resolve(Response.json({ id: "doc-1", name: "scan.png", kind: "document" }));
  await React.act(async () =>
    draft().addFiles([new File([new Uint8Array(3)], "scan.png", { type: "image/png" })]),
  );
  await flush();
  assert.equal(draft().tiles[0]!.status, "failed");
  assert.deepEqual(deleted.slice(before), ["doc-1"]);
  assert.equal(errors.length, errorsBefore + 1);
  await unmount();
});

void test("an original added after the panel closes makes no local URL and starts no upload", async () => {
  const { draft, unmount } = await mountDraft();
  const add = draft().addOriginals;
  await unmount();
  const uploadsBefore = uploads;
  let made = 0;
  const create = URL.createObjectURL;
  URL.createObjectURL = (blob: Blob) => {
    made += 1;
    return create(blob);
  };
  try {
    // The composer reads the file first, so its call can come after the close.
    add([pdf("late.pdf")]);
    await flush();
  } finally {
    URL.createObjectURL = create;
  }
  assert.equal(made, 0);
  assert.equal(uploads, uploadsBefore);
});

void test("a StrictMode remount keeps the draft open for new originals", async () => {
  let latest: ReturnType<typeof useChatMedia> | null = null;
  function Harness() {
    latest = useChatMedia();
    return null;
  }
  const root = createRoot(document.createElement("div"));
  await React.act(async () =>
    root.render(
      <React.StrictMode>
        <Harness />
      </React.StrictMode>,
    ),
  );
  await React.act(async () => latest!.addOriginals([pdf("s.pdf")]));
  await flush();
  assert.deepEqual(
    latest!.shownOriginals([{ name: "s.pdf" }]).map((o) => o.name),
    ["s.pdf"],
  );
  await React.act(async () => root.unmount());
});

void test("Undo after the panel closes frees the file's URL and deletes its stored copy", async () => {
  const { draft, unmount } = await mountDraft();
  const toasts = document.createElement("div");
  document.body.append(toasts);
  const toastRoot = createRoot(toasts);
  await React.act(async () =>
    toastRoot.render(<ToastContainer transition={Instant} autoClose={false} />),
  );
  await React.act(async () => draft().addFiles([image(7)]));
  await flush();
  const [stored] = draft().ready()!;
  await React.act(async () => draft().remove(draft().tiles[0]!.id));
  await flush();
  await unmount();
  const before = deleted.length;
  await recordingRevokes(async (revoked) => {
    const undo = [...toasts.querySelectorAll("button")].find((b) => b.textContent === "Undo");
    assert.ok(undo, "the removed-file toast outlives the panel");
    await React.act(async () => undo.click());
    await flush();
    assert.deepEqual(revoked, [stored!.url]);
  });
  assert.deepEqual(deleted.slice(before), [stored!.id]);
  await React.act(async () => toastRoot.unmount());
  toasts.remove();
});

void test("an upload that fails after the panel closes shows no error", async () => {
  const { draft, unmount } = await mountDraft();
  const errorsBefore = errors.length;
  let fail: () => void = () => {};
  nextUpload = new Promise<Response>(
    (resolve) => (fail = () => resolve(new Response(null, { status: 500 }))),
  );
  await React.act(async () => draft().addFiles([image(8)]));
  await unmount();
  fail();
  await flush();
  assert.equal(errors.length, errorsBefore);
});
