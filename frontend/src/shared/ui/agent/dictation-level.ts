/** Pure helpers for dictation: mic loudness, the bar history and live update timing. */

/** How often the waveform takes one bar from the mic, in milliseconds. */
export const WAVE_STEP_MS = 60;

/** The noise gate of the live-update check: lower than the bars' gate, so a quiet speaker counts as sound. */
export const HEAR_FLOOR = 0.003;

/**
 * Turn one analyser frame (bytes centered on 128) into a bar height from 0 to 1.
 *
 * Args:
 *   frame: Time-domain samples from `AnalyserNode.getByteTimeDomainData`.
 *   floor: The RMS below which the frame reads as silence.
 *
 * Returns:
 *   The loudness of the frame. Speech fills most of the range; room noise stays near 0.
 */
export function frameLevel(frame: Uint8Array, floor = 0.01): number {
  if (!frame.length) return 0;
  let sum = 0;
  for (const byte of frame) {
    const x = (byte - 128) / 128;
    sum += x * x;
  }
  const rms = Math.sqrt(sum / frame.length);
  // Normal speech peaks near 0.1-0.3 RMS. The square root lifts quiet speech
  // so it reads as bars, while the gate keeps room noise flat.
  const gated = Math.max(0, rms - floor);
  return Math.min(1, Math.sqrt(gated * 6));
}

/**
 * Where one bar slot starts in the strip. Slot 0 is at the inline start.
 *
 * Args:
 *   slot: The slot index, from 0.
 *   width: The strip width in CSS pixels.
 *   bar: The bar width.
 *   gap: The space between bars.
 *   rtl: True when the inline start is on the right.
 *
 * Returns:
 *   The x of the slot's left edge.
 */
export function slotX(slot: number, width: number, bar: number, gap: number, rtl: boolean): number {
  return rtl ? width - (slot + 1) * (bar + gap) + gap : slot * (bar + gap);
}

/**
 * Add one bar to the history and keep only the newest `max` bars.
 *
 * Args:
 *   bars: Bar heights, oldest first.
 *   level: The new bar's height, from 0 to 1.
 *   max: How many bars fit in the strip.
 *
 * Returns:
 *   A new array, oldest first, with at most `max` bars.
 */
export function pushBar(bars: readonly number[], level: number, max: number): number[] {
  const next = [...bars, Math.min(1, Math.max(0, level))];
  return next.length > max ? next.slice(next.length - max) : next;
}

const FIRST_PARTIAL_MS = 2500;
// Each live update sends only the audio since the last one, so a fixed gap
// keeps the audio sent for all updates near the length of the take itself.
const PARTIAL_GAP_MS = 3000;

/**
 * Say whether the take is long enough for the next live transcript update.
 *
 * Args:
 *   elapsedMs: How long the take is now.
 *   lastSentMs: How long the take was at the last live update; 0 for none.
 *
 * Returns:
 *   True when a new live update may go to the server.
 */
export function partialDue(elapsedMs: number, lastSentMs: number): boolean {
  return lastSentMs === 0
    ? elapsedMs >= FIRST_PARTIAL_MS
    : elapsedMs - lastSentMs >= PARTIAL_GAP_MS;
}

const WEBM_CLUSTER = [0x1f, 0x43, 0xb6, 0x75];
const WEBM_TIMECODE = 0xe7;
const MP4_MOOF = [0x6d, 0x6f, 0x6f, 0x66];
const MP4_MFHD = [0x6d, 0x66, 0x68, 0x64];

/**
 * Say whether a signature match at `i` is a real unit, not audio bytes that
 * look like one.
 *
 * Args:
 *   bytes: The recording bytes being scanned.
 *   i: Where the signature (the Cluster ID, or the 'moof' type) starts.
 *   mp4: True for an MP4 recording.
 *
 * Returns:
 *   True when the bytes after the signature are those every unit has: a
 *   WebM Cluster's size and then its Timecode ID, or an MP4 fragment's 'mfhd'
 *   box first. False also while those bytes are not recorded yet.
 */
function isUnit(bytes: Uint8Array, i: number, mp4: boolean): boolean {
  if (mp4) return MP4_MFHD.every((b, j) => bytes[i + 8 + j] === b);
  const sizeByte = bytes[i + 4];
  if (!sizeByte) return false;
  // An EBML size takes one byte more for each leading zero bit of its first byte.
  const sizeLength = Math.clz32(sizeByte) - 24 + 1;
  return bytes[i + 4 + sizeLength] === WEBM_TIMECODE;
}

/**
 * Find where each unit of audio starts in a recording.
 *
 * A live update must hold whole units: the server cannot decode audio that
 * starts in the middle of one. A unit is a WebM Cluster, or a fragment box of
 * a fragmented MP4. The bytes before the first unit are the file header.
 *
 * Args:
 *   bytes: The recording so far, or a part of it.
 *   mp4: True for an MP4 recording (Safari); false for WebM.
 *   offset: Where `bytes` starts in the recording, so a caller can scan only
 *     the new bytes. The offsets returned count from the recording's start.
 *
 * Returns:
 *   The byte offsets of the units, in order. Empty while the header is still
 *   all there is. A unit whose first bytes are not all recorded yet is not
 *   in the list.
 */
export function audioStarts(bytes: Uint8Array, mp4: boolean, offset = 0): number[] {
  const sig = mp4 ? MP4_MOOF : WEBM_CLUSTER;
  // An MP4 box starts with its 4-byte size, then its 4-byte type.
  const back = mp4 ? 4 : 0;
  const starts: number[] = [];
  for (let i = back; i + sig.length <= bytes.length; i++) {
    // The file header is at 0, so no unit starts there.
    if (offset + i - back === 0) continue;
    if (sig.every((b, j) => bytes[i + j] === b) && isUnit(bytes, i, mp4)) {
      starts.push(offset + i - back);
    }
  }
  return starts;
}

/** How many bytes a unit's start needs to be known (an MP4 'moof' and 'mfhd' head). */
export const UNIT_HEAD_BYTES = 16;

// Chinese and Japanese put no space between words, so their parts join with none.
const NO_SPACE_SCRIPT = /[\u3000-\u9fff\uff00-\uffef]/;

/**
 * Join the words of the live updates in order.
 *
 * Args:
 *   parts: The trimmed, non-empty text of each live update, oldest first.
 *
 * Returns:
 *   The parts with a space between them, or with none where either side of
 *   the join is Chinese or Japanese script.
 */
export function joinHeard(parts: readonly string[]): string {
  return parts.reduce((text, part) => {
    if (!text) return part;
    const tight = NO_SPACE_SCRIPT.test(text.at(-1)!) || NO_SPACE_SCRIPT.test(part[0]!);
    return text + (tight ? "" : " ") + part;
  }, "");
}

/**
 * Tell whether this browser can show live dictation words.
 *
 * Firefox before 125 has no Intl.Segmenter. A space split finds no words in
 * Chinese and Japanese, so those browsers show no live words: the take sends
 * no live updates, and only the final transcript lands in the message box.
 *
 * Returns:
 *   True when Intl.Segmenter exists.
 */
export function canShowLiveWords(): boolean {
  return typeof Intl.Segmenter === "function";
}

// Built on first use: a throw at module load would break the whole composer
// in a browser with no Intl.Segmenter.
let words: Intl.Segmenter | undefined;

/** Split text into word pieces with the word segmenter. */
function segments(text: string): Array<{ segment: string; isWordLike?: boolean }> {
  words ??= new Intl.Segmenter(undefined, { granularity: "word" });
  return [...words.segment(text)];
}

/**
 * Split the live words into pieces, and mark the last word and what follows it.
 *
 * Args:
 *   text: The joined words of the live updates.
 *
 * Returns:
 *   The pieces in order. `tail` is true from the last word on, or for every
 *   piece when the text holds no word.
 */
export function liveWords(text: string): Array<{ piece: string; tail: boolean }> {
  const pieces = segments(text);
  const last = Math.max(
    0,
    pieces.findLastIndex((p) => p.isWordLike),
  );
  return pieces.map((p, i) => ({ piece: p.segment, tail: i >= last }));
}
