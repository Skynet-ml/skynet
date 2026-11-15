/*
 * node --test cannot load .tsx, so this runs through jiti (media-attachments.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { isUnsupportedImage, loadPeaks, mediaKind, PEAKS_MAX_BYTES } from "./media-attachments";

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

void describe("audio waveform", () => {
  /** A decoder that counts its calls and returns a short ramp of samples. */
  function counting() {
    const calls: number[] = [];
    const decode = async (buffer: ArrayBuffer) => {
      calls.push(buffer.byteLength);
      return { getChannelData: () => Float32Array.from({ length: 64 }, (_, i) => i / 64) };
    };
    return { calls, decode };
  }

  void it("draws a flat line for a file over the cap and never decodes it", async () => {
    const { calls, decode } = counting();
    const url = URL.createObjectURL(new Blob([new Uint8Array(PEAKS_MAX_BYTES + 1)]));
    try {
      assert.deepEqual(await loadPeaks(url, 4, decode), [0.3, 0.3, 0.3, 0.3]);
    } finally {
      URL.revokeObjectURL(url);
    }
    assert.deepEqual(calls, []);
  });

  void it("decodes a file under the cap into scaled peaks", async () => {
    const { calls, decode } = counting();
    const url = URL.createObjectURL(new Blob([new Uint8Array(8)]));
    try {
      const peaks = await loadPeaks(url, 4, decode);
      assert.equal(peaks.length, 4);
      assert.equal(Math.max(...peaks), 1);
    } finally {
      URL.revokeObjectURL(url);
    }
    assert.deepEqual(calls, [8]);
  });
});
