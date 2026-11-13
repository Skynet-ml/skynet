"use client";

import * as React from "react";

import { ApiError, transcribeAudio } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";

import {
  UNIT_HEAD_BYTES,
  audioStarts,
  canShowLiveWords,
  joinHeard,
  partialDue,
} from "./dictation-level";

/** The composer's voice state: recording live, transcribing the take, or
 *  showing a short-lived failure before returning to idle. */
export type DictationState =
  | { kind: "idle" }
  | { kind: "rec" }
  | { kind: "busy" }
  | { kind: "err"; message: string };

const ERR_DISMISS_MS = 4500;
// A reason that "try again" won't fix needs long enough to be read.
const REASON_DISMISS_MS = 6000;
// Live updates that fail this many times in a row stop for the rest of the take.
const MAX_LIVE_FAILURES = 3;
// The wave reads a level each frame (16 to 33 ms). A longer gap means it stopped
// reading (a hidden tab or a stall), so the level is stale. A false "sound" only
// sends one more update, so the limit stays short.
const FRESH_LEVEL_MS = 200;
// A chunk holds no audio from before the previous chunk arrived, apart from
// the encoder's delay.
const ENCODER_MARGIN_MS = 1000;

/** The backend's own reason when retrying can't help (no key, not eligible,
 *  over a cap, too large); null for a transient failure. */
function blockedReason(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  // The hourly clip cap answers with the shared limiter's code, not a transcription one.
  if (err.code === "rate_limit.exceeded") return err.message;
  if (!err.code?.startsWith("transcription.")) return null;
  return err.code === "transcription.failed" ? null : err.message;
}

/**
 * Record → transcribe → hand the text back for the draft. The transcript
 * only lands in the caller's input — it never fires a send on its own.
 * Safari records AAC-in-MP4, everywhere else webm/opus; the clip goes to
 * ``POST /transcribe`` with the UI locale as a soft language hint.
 */
export function useDictation({
  onText,
  language,
}: {
  /** Receives the finished transcript (trimmed, non-empty). */
  onText: (text: string) => void;
  /** BCP-47 UI locale forwarded as a soft STT hint. */
  language?: string;
}) {
  const [state, setState] = React.useState<DictationState>({ kind: "idle" });
  const [seconds, setSeconds] = React.useState(0);
  // The live mic stream, so the composer can draw its level while recording.
  const [stream, setStream] = React.useState<MediaStream | null>(null);
  // Words heard so far, from the live updates; replaced by the final transcript.
  const [live, setLive] = React.useState("");
  // True while the browser asks for the mic, so a second click cannot start a second take.
  const [starting, setStarting] = React.useState(false);
  const startingRef = React.useRef(false);
  // Teardown moves this on, so a take whose mic prompt resolves late knows it is stale.
  const takeRef = React.useRef(0);
  const partialRef = React.useRef<AbortController | null>(null);
  const finalRef = React.useRef<AbortController | null>(null);
  const recorderRef = React.useRef<MediaRecorder | null>(null);
  const streamRef = React.useRef<MediaStream | null>(null);
  const chunksRef = React.useRef<Blob[]>([]);
  const mimeRef = React.useRef("audio/webm;codecs=opus");
  const tickRef = React.useRef<number | undefined>(undefined);
  const dismissRef = React.useRef<number | undefined>(undefined);
  // start() is declared first, so its stop handler reaches these through refs.
  const settleRef = React.useRef<() => void>(() => {});
  const sendTakeRef = React.useRef<(take: number) => void>(() => {});
  // When the waveform last read the mic level, and when that level last held sound.
  const soundRef = React.useRef({ read: 0, loud: 0 });
  const onTextRef = React.useRef(onText);
  React.useEffect(() => {
    onTextRef.current = onText;
  }, [onText]);

  const teardown = React.useCallback(() => {
    takeRef.current += 1;
    window.clearInterval(tickRef.current);
    partialRef.current?.abort();
    partialRef.current = null;
    // The user left this take, so drop its reply. The server still runs and pays for it.
    finalRef.current?.abort();
    finalRef.current = null;
    setLive("");
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.ondataavailable = null;
      recorder.onstop = null;
      recorder.stop();
    }
    recorderRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    setStream(null);
  }, []);

  const fail = React.useCallback(
    (message: string, dismissMs = ERR_DISMISS_MS) => {
      teardown();
      setState({ kind: "err", message });
      // A new failure restarts the timer, so an old one cannot cut its message short.
      window.clearTimeout(dismissRef.current);
      dismissRef.current = window.setTimeout(
        () => setState((s) => (s.kind === "err" ? { kind: "idle" } : s)),
        dismissMs,
      );
    },
    [teardown],
  );

  const start = React.useCallback(async () => {
    if (recorderRef.current || startingRef.current) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      fail(msg("agent.composer.mic_unavailable"));
      return;
    }
    const take = takeRef.current;
    startingRef.current = true;
    setStarting(true);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      if (take === takeRef.current) fail(msg("agent.composer.mic_unavailable"));
      return;
    } finally {
      startingRef.current = false;
      setStarting(false);
    }
    // An unmount during the mic prompt already ran teardown; this take has no owner.
    if (take !== takeRef.current) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    // Safari records AAC-in-MP4, everywhere else webm/opus.
    const isSafari = /apple/i.test(navigator.vendor);
    const candidates = isSafari
      ? ["audio/mp4", "audio/webm;codecs=opus"]
      : ["audio/webm;codecs=opus", "audio/mp4"];
    const mime =
      candidates.find((c) => MediaRecorder.isTypeSupported(c)) ?? "audio/webm;codecs=opus";
    mimeRef.current = mime;
    // Own the stream first, so a failure below lets teardown release the mic.
    streamRef.current = stream;
    setStream(stream);
    let recorder: MediaRecorder;
    try {
      recorder = new MediaRecorder(stream, { mimeType: mime });
    } catch {
      try {
        recorder = new MediaRecorder(stream);
      } catch {
        fail(msg("agent.composer.mic_unavailable"));
        return;
      }
    }
    recorderRef.current = recorder;
    chunksRef.current = [];
    // When each chunk arrived, by the byte offset where it ends. A unit's audio
    // starts no earlier than the arrival of the chunk before the one with its
    // first byte, whatever the unit's length (Chrome's WebM clusters may span
    // seconds) and however late the recorder hands a chunk over.
    const t0 = Date.now();
    const arrivals: Array<{ end: number; at: number }> = [];
    let recorded = 0;
    recorder.ondataavailable = (ev) => {
      if (!ev.data || ev.data.size <= 0) return;
      chunksRef.current.push(ev.data);
      recorded += ev.data.size;
      arrivals.push({ end: recorded, at: Date.now() });
    };
    const audioFrom = (offset: number) => {
      const i = arrivals.findIndex((chunk) => chunk.end > offset);
      return i <= 0 ? t0 : arrivals[i - 1]!.at;
    };
    // The browser stops the recorder by itself when the mic goes away (unplugged,
    // permission revoked). Send what was recorded, as Finish does; finish()
    // replaces this handler for a stop the user asks for.
    recorder.onstop = () => {
      if (recorderRef.current !== recorder) return;
      const take = takeRef.current;
      settleRef.current();
      sendTakeRef.current(take);
    };
    // The take start counts as a read, so a first read that comes late (a slow
    // audio start) marks the gap as sound, as a hidden tab does.
    soundRef.current = { read: Date.now(), loud: 0 };
    setSeconds(0);
    setLive("");
    let lastSent = 0;
    // The bytes that a live update already turned into words (always a unit
    // start, or 0), and those words in order.
    let heardBytes = 0;
    const heard: string[] = [];
    // The header ends where the first unit starts (0 until then). The last unit
    // found may still grow. The scan reads only the bytes after `scanned`.
    let headerEnd = 0;
    let lastStart = 0;
    const unitStarts: number[] = [];
    let scanned = 0;
    // A reason that retrying cannot fix (no key, budget spent) ends live updates.
    // A browser that cannot show live words sends none.
    let livePaused = !canShowLiveWords();
    // A provider outage would fail every update, so a run of failures ends them too.
    let failures = 0;
    // Whisper cannot resume a clip, so each live update sends the file header and
    // only the whole audio units since the last one that worked. The last unit
    // may still grow, so it waits for the next update. The words join in order.
    const sendPartial = async (elapsed: number) => {
      lastSent = elapsed;
      const ctrl = new AbortController();
      partialRef.current = ctrl;
      const mp4 = mimeRef.current.startsWith("audio/mp4");
      const take = new Blob(chunksRef.current);
      // The last scan may have stopped inside a unit's head, so it reads that part again.
      const from = Math.max(0, scanned - UNIT_HEAD_BYTES);
      const bytes = new Uint8Array(
        await take
          .slice(from)
          .arrayBuffer()
          .catch(() => new ArrayBuffer(0)),
      );
      if (bytes.length) {
        for (const at of audioStarts(bytes, mp4, from)) {
          if (at <= lastStart) continue;
          if (!headerEnd) headerEnd = at;
          unitStarts.push(at);
          lastStart = at;
        }
        scanned = from + bytes.length;
      }
      const end = lastStart;
      if (livePaused || ctrl.signal.aborted || end <= Math.max(heardBytes, headerEnd)) {
        if (partialRef.current === ctrl) partialRef.current = null;
        return;
      }
      // Whisper makes up words ("Thank you.") for a clip of silence. When the
      // wave reads the mic and heard no sound in these units, they count as
      // heard with no words. With no fresh level, every update goes out.
      const sentAt = Date.now();
      const sound = soundRef.current;
      const first = Math.max(heardBytes, headerEnd);
      // An MP4 fragment's moof lists all its samples, so the browser writes it
      // after the fragment's audio ends. The unit before it starts no later
      // than this unit's audio does.
      const unitAt = mp4 ? (unitStarts.findLast((at) => at < first) ?? 0) : first;
      const unitFrom = audioFrom(unitAt) - ENCODER_MARGIN_MS;
      if (sentAt - sound.read < FRESH_LEVEL_MS && sound.loud < unitFrom) {
        heardBytes = end;
        if (partialRef.current === ctrl) partialRef.current = null;
        return;
      }
      const parts =
        heardBytes === 0
          ? [take.slice(0, end)]
          : [take.slice(0, headerEnd), take.slice(heardBytes, end)];
      const blob = new Blob(parts, { type: mimeRef.current });
      const filename = mp4 ? "take.m4a" : "take.webm";
      transcribeAudio(blob, filename, language, { partial: true, signal: ctrl.signal })
        .then(({ text, partial }) => {
          failures = 0;
          // An older backend counts each preview against the clip cap, which the
          // final call needs, so the take sends no more previews.
          if (partial !== true) livePaused = true;
          if (ctrl.signal.aborted || recorderRef.current !== recorder) return;
          heardBytes = end;
          const words = (text ?? "").trim();
          if (words) heard.push(words);
          setLive(joinHeard(heard));
        })
        .catch((err: unknown) => {
          // A long hidden tab slows the tick to about once a minute, so the units
          // can outgrow the live cap. They count as heard; the final clip holds them.
          if (err instanceof ApiError && err.code === "transcription.too_large") {
            if (recorderRef.current === recorder) heardBytes = end;
            return;
          }
          if (blockedReason(err)) livePaused = true;
          if (!ctrl.signal.aborted && ++failures >= MAX_LIVE_FAILURES) livePaused = true;
        })
        .finally(() => {
          if (partialRef.current === ctrl) partialRef.current = null;
        });
    };
    tickRef.current = window.setInterval(() => {
      const elapsed = Date.now() - t0;
      setSeconds(Math.round(elapsed / 1000));
      if (
        !livePaused &&
        !partialRef.current &&
        chunksRef.current.length &&
        partialDue(elapsed, lastSent)
      ) {
        void sendPartial(elapsed);
      }
    }, 500);
    setState({ kind: "rec" });
    try {
      recorder.start(1000);
      // The browser may record another type than the one asked for, or pick
      // one itself when the recorder had no type; the file name follows it.
      mimeRef.current = recorder.mimeType || mime;
    } catch {
      fail(msg("agent.composer.mic_unavailable"));
    }
  }, [fail, language]);

  const cancel = React.useCallback(() => {
    teardown();
    chunksRef.current = [];
    setState({ kind: "idle" });
  }, [teardown]);

  // Send the stopped take for the final transcript. The take is read before the
  // stop event, so an unmount or a failure in that gap drops the clip unsent.
  const sendTake = React.useCallback(
    (take: number) => {
      if (take !== takeRef.current) return;
      const blob = new Blob(chunksRef.current, { type: mimeRef.current });
      chunksRef.current = [];
      streamRef.current?.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
      setStream(null);
      recorderRef.current = null;
      const ctrl = new AbortController();
      finalRef.current = ctrl;
      void (async () => {
        try {
          const filename = mimeRef.current.startsWith("audio/mp4") ? "take.m4a" : "take.webm";
          const { text } = await transcribeAudio(blob, filename, language, {
            signal: ctrl.signal,
          });
          if (take !== takeRef.current) return;
          const clean = (text ?? "").trim();
          // Silence or a decode miss isn't a draft — surface it as a
          // retryable failure instead of appending nothing.
          if (!clean) throw new Error("empty transcript");
          setState({ kind: "idle" });
          setLive("");
          onTextRef.current(clean);
        } catch (err) {
          if (take !== takeRef.current) return;
          const reason = blockedReason(err);
          if (reason) fail(reason, REASON_DISMISS_MS);
          else fail(msg("agent.composer.transcribe_failed"));
        } finally {
          if (finalRef.current === ctrl) finalRef.current = null;
        }
      })();
    },
    [fail, language],
  );

  // Stop the live updates and show the take as busy until its transcript returns.
  const settle = React.useCallback(() => {
    window.clearInterval(tickRef.current);
    // The final request replaces any live update still on its way.
    partialRef.current?.abort();
    partialRef.current = null;
    // The waveform freezes on the take as soon as the user stops talking.
    setStream(null);
    setState({ kind: "busy" });
  }, []);
  React.useEffect(() => {
    settleRef.current = settle;
    sendTakeRef.current = sendTake;
  }, [settle, sendTake]);

  const finish = React.useCallback(() => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === "inactive") return;
    const take = takeRef.current;
    recorder.onstop = () => sendTake(take);
    settle();
    recorder.stop();
  }, [sendTake, settle]);

  // The waveform reports each mic level it reads, so a silent stretch sends no live update.
  const hearLevel = React.useCallback((level: number) => {
    const now = Date.now();
    // After a gap in the reads (a hidden tab), the take may hold unheard speech.
    if (soundRef.current.read && now - soundRef.current.read >= FRESH_LEVEL_MS)
      soundRef.current.loud = now;
    soundRef.current.read = now;
    if (level > 0) soundRef.current.loud = now;
  }, []);

  // A recording left running when the host unmounts would hold the mic open.
  React.useEffect(
    () => () => {
      teardown();
      window.clearTimeout(dismissRef.current);
    },
    [teardown],
  );

  return { state, starting, seconds, stream, live, start, cancel, finish, hearLevel };
}

/** mm:ss for the live recording timer. */
export function formatRecSeconds(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
