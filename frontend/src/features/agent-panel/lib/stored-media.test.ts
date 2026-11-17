import assert from "node:assert/strict";
import { test } from "node:test";

import { parseStoredMedia } from "./stored-media.ts";

test("parseStoredMedia reads the files of a stored user turn with an empty URL", () => {
  assert.deepEqual(
    parseStoredMedia([
      { id: "m-1", name: "shot.png", kind: "image" },
      { id: "m-2", name: "clip.mp4", kind: "video" },
    ]),
    [
      { id: "m-1", name: "shot.png", kind: "image", url: "" },
      { id: "m-2", name: "clip.mp4", kind: "video", url: "" },
    ],
  );
});

test("parseStoredMedia drops bad entries and returns undefined when nothing is left", () => {
  assert.equal(parseStoredMedia(null), undefined);
  assert.equal(parseStoredMedia("x"), undefined);
  assert.equal(
    parseStoredMedia([
      null,
      { id: 1, name: "a", kind: "image" },
      { id: "m", name: "a", kind: "pdf" },
    ]),
    undefined,
  );
});

test("parseStoredMedia keeps the file card a document original belongs to (SKY-782)", () => {
  assert.deepEqual(
    parseStoredMedia([
      { id: "m-1", name: "brief.pdf", kind: "document", card: "c-1" },
      { id: "m-2", name: "old.pdf", kind: "document" },
      { id: "m-3", name: "shot.png", kind: "image", card: "c-3" },
      { id: "m-4", name: "bad.pdf", kind: "document", card: 4 },
    ]),
    [
      { id: "m-1", name: "brief.pdf", kind: "document", url: "", card: "c-1" },
      { id: "m-2", name: "old.pdf", kind: "document", url: "" },
      { id: "m-3", name: "shot.png", kind: "image", url: "" },
      { id: "m-4", name: "bad.pdf", kind: "document", url: "" },
    ],
  );
});
