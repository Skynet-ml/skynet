/*
 * Checks that the stored-file URL cache frees its object URLs. chat-media.render.test.ts
 * runs this file through jiti inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { test } from "node:test";

let made = 0;
const revoked: string[] = [];
URL.createObjectURL = () => `blob:test/${++made}`;
URL.revokeObjectURL = (url: string) => void revoked.push(url);
globalThis.fetch = (async () => new Response(new Blob(["x"]))) as typeof fetch;

const { freeChatMediaUrls, localMediaUrl, markMediaSent, resolveChatMediaUrl } = await import("./chat-media");

void test("a chat switch frees fetched and sent URLs and keeps draft URLs", async () => {
  const draft = localMediaUrl(new Blob(["d"]));
  const sent = localMediaUrl(new Blob(["s"]));
  markMediaSent([sent]);
  assert.equal(await resolveChatMediaUrl({ id: "s1", name: "s.png", kind: "image", url: sent }), sent);
  const first = await resolveChatMediaUrl({ id: "m1", name: "a.png", kind: "image", url: "blob:old/1" });
  assert.equal(await resolveChatMediaUrl({ id: "m1", name: "a.png", kind: "image", url: "" }), first);
  const late = resolveChatMediaUrl({ id: "m2", name: "b.png", kind: "image", url: "" });

  freeChatMediaUrls();
  const lateUrl = await late;
  await Promise.resolve();

  assert.deepEqual(revoked.sort(), [first, lateUrl, sent].sort());
  assert.ok(!revoked.includes(draft));
  const again = await resolveChatMediaUrl({ id: "m1", name: "a.png", kind: "image", url: "" });
  assert.notEqual(again, first);
});
