/*
 * node --test cannot load .tsx, so this runs through jiti (media-attachments.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { isUnsupportedImage, mediaKind } from "./media-attachments";

void describe("media routing", () => {
  void it("sends an SVG down the text attachment path", () => {
    assert.equal(mediaKind({ type: "image/svg+xml" }), null);
    assert.equal(mediaKind({ type: "image/png" }), "image");
    assert.equal(mediaKind({ type: "video/mp4" }), "video");
    assert.equal(mediaKind({ type: "audio/mpeg" }), "audio");
  });

  void it("marks HEIC, HEIF and AVIF photos as unsupported, also with no type", () => {
    assert.equal(isUnsupportedImage({ type: "image/heic", name: "photo" }), true);
    assert.equal(isUnsupportedImage({ type: "image/heif", name: "photo" }), true);
    assert.equal(isUnsupportedImage({ type: "image/avif", name: "photo" }), true);
    assert.equal(isUnsupportedImage({ type: "", name: "IMG_0001.HEIC" }), true);
    assert.equal(isUnsupportedImage({ type: "image/png", name: "shot.png" }), false);
  });
});
