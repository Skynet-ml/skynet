"use client";

import * as React from "react";

import { ApiError, transcribeAudio } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";

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
  // True while the browser asks for the mic, so a second click cannot start a second take.
  const [starting, setStarting] = React.useState(false);
  const startingRef = React.useRef(false);
  // Teardown moves this on, so a take whose mic prompt resolves late knows it is stale.
  const takeRef = React.useRef(0);
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
  const onTextRef = React.useRef(onText);
  React.useEffect(() => {
    onTextRef.current = onText;
  }, [onText]);

  const teardown = React.useCallback(() => {
    takeRef.current += 1;
    window.clearInterval(tickRef.current);
    // The user left this take, so drop its reply. The server still runs and pays for it.
    finalRef.current?.abort();
    finalRef.current = null;
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
    recorder.ondataavailable = (ev) => {
      if (ev.data && ev.data.size > 0) chunksRef.current.push(ev.data);
    };
    const t0 = Date.now();
    // The browser stops the recorder by itself when the mic goes away (unplugged,
    // permission revoked). Send what was recorded, as Finish does; finish()
    // replaces this handler for a stop the user asks for.
    recorder.onstop = () => {
      if (recorderRef.current !== recorder) return;
      const take = takeRef.current;
      settleRef.current();
      sendTakeRef.current(take);
    };
    setSeconds(0);
    tickRef.current = window.setInterval(
      () => setSeconds(Math.round((Date.now() - t0) / 1000)),
      500,
    );
    setState({ kind: "rec" });
    try {
      recorder.start(1000);
      // The browser may record another type than the one asked for, or pick
      // one itself when the recorder had no type; the file name follows it.
      mimeRef.current = recorder.mimeType || mime;
    } catch {
      fail(msg("agent.composer.mic_unavailable"));
    }
  }, [fail]);

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

  // Stop the timer and show the take as busy until its transcript returns.
  const settle = React.useCallback(() => {
    window.clearInterval(tickRef.current);
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

  // A recording left running when the host unmounts would hold the mic open.
  React.useEffect(
    () => () => {
      teardown();
      window.clearTimeout(dismissRef.current);
    },
    [teardown],
  );

  return { state, starting, seconds, stream, start, cancel, finish };
}

/** mm:ss for the live recording timer. */
export function formatRecSeconds(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
