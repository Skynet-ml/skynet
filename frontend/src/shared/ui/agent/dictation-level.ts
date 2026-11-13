/** Pure helper for the dictation wave: the mic loudness of one analyser frame. */

// The RMS below which a frame reads as silence, so room noise keeps the line flat.
const SILENCE_FLOOR = 0.01;

/**
 * Turn one analyser frame (bytes centered on 128) into a level from 0 to 1.
 *
 * Args:
 *   frame: Time-domain samples from `AnalyserNode.getByteTimeDomainData`.
 *
 * Returns:
 *   The loudness of the frame. Speech fills most of the range; room noise stays near 0.
 */
export function frameLevel(frame: Uint8Array): number {
  if (!frame.length) return 0;
  let sum = 0;
  for (const byte of frame) {
    const x = (byte - 128) / 128;
    sum += x * x;
  }
  const rms = Math.sqrt(sum / frame.length);
  // Normal speech peaks near 0.1-0.3 RMS. The square root lifts quiet speech
  // so it moves the line, while the gate keeps room noise flat.
  const gated = Math.max(0, rms - SILENCE_FLOOR);
  return Math.min(1, Math.sqrt(gated * 6));
}
