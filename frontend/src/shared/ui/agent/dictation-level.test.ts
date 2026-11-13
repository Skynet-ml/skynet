/** Tests for the dictation waveform helpers. */

import assert from "node:assert/strict";
import test from "node:test";

import {
  HEAR_FLOOR,
  audioStarts,
  frameLevel,
  liveWords,
  partialDue,
  pushBar,
  slotX,
} from "./dictation-level.ts";

const frame = (amp: number, n = 512) =>
  Uint8Array.from({ length: n }, (_, i) => Math.round(128 + amp * 127 * Math.sin(i / 4)));

test("silence and room noise stay flat", () => {
  assert.equal(frameLevel(new Uint8Array(0)), 0);
  assert.equal(frameLevel(frame(0)), 0);
  assert.ok(frameLevel(frame(0.01)) < 0.05);
});

test("louder speech gives a taller bar, capped at 1", () => {
  const quiet = frameLevel(frame(0.1));
  const loud = frameLevel(frame(0.4));
  assert.ok(quiet > 0.2, `quiet speech shows as a bar (${quiet})`);
  assert.ok(loud > quiet);
  assert.equal(frameLevel(frame(1)), 1);
});

test("the bar history keeps only the newest bars that fit", () => {
  assert.deepEqual(pushBar([], 0.5, 3), [0.5]);
  assert.deepEqual(pushBar([0.1, 0.2, 0.3], 0.4, 3), [0.2, 0.3, 0.4]);
  assert.deepEqual(pushBar([0.1], 2, 3), [0.1, 1]);
  assert.deepEqual(pushBar([0.1], -1, 3), [0.1, 0]);
});

test("live updates start after a short pause and then come every 3 s", () => {
  assert.equal(partialDue(2000, 0), false);
  assert.equal(partialDue(2500, 0), true);
  assert.equal(partialDue(5000, 2500), false, "at least 3 s apart");
  assert.equal(partialDue(5500, 2500), true);
  assert.equal(partialDue(122_000, 120_000), false, "the gap does not grow");
  assert.equal(partialDue(123_000, 120_000), true);
});

test("audio units start at each WebM cluster or MP4 fragment", () => {
  // A Cluster ID, a 1-byte size, then the Timecode ID that starts every cluster.
  const C = [0x1f, 0x43, 0xb6, 0x75, 0x81, 0xe7];
  const webm = Uint8Array.from([0x1a, 0x45, 0xdf, 0xa3, 9, 9, ...C, 1, 2, ...C, 3]);
  assert.deepEqual(audioStarts(webm, false), [6, 14]);
  assert.deepEqual(
    audioStarts(Uint8Array.from([...C, 1]), false),
    [],
    "no header before the audio",
  );
  const text = (s: string) => new TextEncoder().encode(s);
  const head = new Uint8Array([0, 0, 0, 8, ...text("ftyp"), 0, 0, 0, 9, ...text("moov")]);
  assert.deepEqual(audioStarts(head, true), [], "no fragment yet");
  const frag = [0, 0, 0, 17, ...text("moof"), 0, 0, 0, 16, ...text("mfhd"), 1];
  assert.deepEqual(audioStarts(new Uint8Array([...head, ...frag, ...frag]), true), [16, 33]);
  // Each format looks only for its own unit start.
  assert.deepEqual(audioStarts(Uint8Array.from([9, ...C, 1]), true), []);
  assert.deepEqual(audioStarts(new Uint8Array([...head, ...frag]), false), []);
});

test("audio bytes that look like a unit start are not one", () => {
  const ID = [0x1f, 0x43, 0xb6, 0x75];
  // The Cluster ID inside the audio, with no Timecode after its size.
  assert.deepEqual(audioStarts(Uint8Array.from([9, 9, ...ID, 0x81, 0x12, 3]), false), []);
  // MediaRecorder writes an 8-byte unknown size, so the Timecode is 8 bytes on.
  const big = [...ID, 0x01, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xe7];
  assert.deepEqual(audioStarts(Uint8Array.from([9, 9, ...big, 1]), false), [2]);
  assert.deepEqual(
    audioStarts(Uint8Array.from([9, 9, ...big.slice(0, -1)]), false),
    [],
    "head not all recorded",
  );
  const text = (s: string) => new TextEncoder().encode(s);
  assert.deepEqual(
    audioStarts(Uint8Array.from([9, 0, 0, 0, 9, ...text("moof"), 1, 2, 3, 4, 5, 6, 7, 8]), true),
    [],
  );
});

test("a scan of the new bytes alone gives offsets from the recording's start", () => {
  const C = [0x1f, 0x43, 0xb6, 0x75, 0x81, 0xe7];
  const all = Uint8Array.from([0x1a, 0x45, 9, ...C, 1, 2, ...C, 3, ...C, 4]);
  assert.deepEqual(audioStarts(all, false), [3, 11, 18]);
  assert.deepEqual(audioStarts(all.subarray(11), false, 11), [11, 18]);
  assert.deepEqual(audioStarts(Uint8Array.from(C), false, 0), [], "nothing starts at 0");
});

test("bar slots run from the inline start and mirror in RTL", () => {
  // 25 slots of a 2px bar and a 2px gap fill a 100px strip.
  assert.equal(slotX(0, 100, 2, 2, false), 0);
  assert.equal(slotX(24, 100, 2, 2, false), 96);
  assert.equal(slotX(0, 100, 2, 2, true), 98);
  assert.equal(slotX(24, 100, 2, 2, true), 2);
});

test("only the last live word stays muted, in Japanese and Chinese too", () => {
  const tails = (text: string) =>
    liveWords(text)
      .filter((p) => p.tail)
      .map((p) => p.piece)
      .join("");
  assert.equal(tails("hello there"), "there");
  assert.equal(tails("hello there."), "there.");
  assert.equal(tails("今日は晴れです"), "です");
  assert.equal(tails("你好世界"), "世界");
  assert.equal(tails("…"), "…");
  assert.equal(
    liveWords("你好世界")
      .map((p) => p.piece)
      .join(""),
    "你好世界",
  );
});

test("a quiet speaker below the bars' gate still counts as sound for live updates", () => {
  assert.equal(frameLevel(frame(0.01)), 0);
  assert.ok(frameLevel(frame(0.01), HEAR_FLOOR) > 0);
  assert.equal(frameLevel(frame(0.003), HEAR_FLOOR), 0);
});

test("with no Intl.Segmenter (Firefox before 125), the module loads and splits on spaces", async () => {
  const segmenter = Intl.Segmenter;
  Object.assign(Intl, { Segmenter: undefined });
  try {
    // A fresh copy of the module, so its segmenter is not built yet.
    const fresh = (await import(
      "./dictation-level.ts?no-segmenter"
    )) as typeof import("./dictation-level.ts");
    assert.deepEqual(fresh.liveWords("hello there."), [
      { piece: "hello", tail: false },
      { piece: " ", tail: false },
      { piece: "there.", tail: true },
    ]);
  } finally {
    Object.assign(Intl, { Segmenter: segmenter });
  }
});
