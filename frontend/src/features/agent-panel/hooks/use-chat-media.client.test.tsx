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
globalThis.fetch = (async (_url: string, init?: RequestInit) => {
  if (init?.method === "DELETE") {
    deleted.push(String(_url).split("/").pop() ?? "");
    return new Response(null, { status: 204 });
  }
  uploads += 1;
  return Response.json({ id: `s-${uploads}`, name: "f.png", kind: "image" });
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
  const sent = draft().ready()!.map((m) => m.url);
  await React.act(async () => draft().clear());
  assert.equal(draft().tiles.length, 0);
  assert.deepEqual(revoked, []);

  freeChatMediaUrls();
  assert.deepEqual(revoked, sent);
  URL.revokeObjectURL = revoke;
  await React.act(async () => root.unmount());
});
