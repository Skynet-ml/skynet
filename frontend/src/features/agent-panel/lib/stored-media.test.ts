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
