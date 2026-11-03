"use client";

import * as React from "react";
import { toast } from "react-toastify";

import {
  localAgentEffort,
  localAgentModel,
  localModelRefusalMessage,
} from "@/features/local-runtime";
import { agentSubscriptionFor, readPref } from "@/features/settings";
import { useAgentKickoff } from "@/shared/hooks/use-agent-kickoff";
import { useCompletionNotification } from "@/shared/hooks/use-completion-notification";
import { streamTaskInterviewTurn } from "@/shared/lib/api";
import { LOCALE_RELOAD_EVENT } from "@/shared/lib/locale";
import { liveEffort, sentFastMode } from "@/shared/lib/effort-slider";
import { msg } from "@/shared/lib/messages";
import { getActiveLocale } from "@/shared/lib/runtime-locale";
import type {
  InterviewOption,
  TaskInterviewTurnResult,
  UnifiedChatTurn,
} from "@/shared/types/unified-run";
import type { AgentMessage, AgentThinking } from "@/shared/ui/agent";

import { fitsInterviewTurn, INTERVIEW_TURN_MAX_CHARS } from "../lib/interview-turn";
import { taskInterviewPatch, type TaskSlice, type WizardV2Context } from "../lib/wizard-v2";

/** Task fields the interview writes and the user may take over by editing. */
export type TaskInterviewField = "objective" | "background" | "artifactKind" | "startingVersion";

interface ServedTurn {
  model: string | null;
  servedModel: string | null;
}

export interface TaskInterviewState {
  messages: AgentMessage[];
  busy: boolean;
  streamText: string;
  thinking: AgentThinking | null;
  /** Pickable answers for the current question; empty for an open question. */
  options: InterviewOption[];
  error: boolean;
  /** A specific message for `error`, such as the local backend's no-model refusal. */
  errorMessage: string | null;
  /** The interviewer has what it needs; the artifact card is complete. */
  done: boolean;
  model: string | null;
  setModel: (model: string | null) => void;
  reasoningEffort: string | null;
  setReasoningEffort: (effort: string | null) => void;
  /** Runs a local CLI model in its fast mode. */
  fastMode: boolean;
  setFastMode: (fast: boolean) => void;
  /** Send a user turn; false when it is too long and was not sent. */
  send: (content: string) => boolean;
  /** Resend an edited user turn; false when it is too long and was not sent. */
  editAndResend: (messageIndex: number, content: string) => boolean;
  stop: () => void;
  /** Re-run the last turn: after an error, a stop, or a reload mid-turn. */
  retry: () => void;
  /** Clear the transcript and start over; hand-edited fields stay as they are. */
  restart: () => void;
  /** Write a field by hand; the interview never overwrites it afterwards. */
  editField: <K extends TaskInterviewField>(field: K, value: TaskSlice[K]) => void;
  isDirty: (field: TaskInterviewField) => boolean;
}

/** Check a user turn's size; a turn over the limit is never saved, and the user is told why. */
export function fitsInterview(content: string): boolean {
  if (fitsInterviewTurn(content)) return true;
  toast.error(
    msg("submit.code.interview.too_long", {
      count: content.length,
      max: INTERVIEW_TURN_MAX_CHARS,
    }),
  );
  return false;
}

/**
 * Step 1's interview: asks what the user wants to optimize and, turn by
 * turn, writes the objective, background, artifact kind and starting version
 * into the task slice. The transcript lives in the wizard state so it
 * survives leaving the step; the live stream and the model attribution are
 * local.
 */
export function useTaskInterview(ctx: WizardV2Context, enabled: boolean): TaskInterviewState {
  const { state, patchTask } = ctx;
  const task = state.task;
  const turns = task.transcript;

  const [busy, setBusy] = React.useState(false);
  useCompletionNotification(busy, () => msg("notify.interview.turn"));
  const [streamText, setStreamText] = React.useState("");
  const [thinking, setThinking] = React.useState<AgentThinking | null>(null);
  const [options, setOptions] = React.useState<InterviewOption[]>([]);
  const [error, setError] = React.useState(false);
  const [errorMessage, setErrorMessage] = React.useState<string | null>(null);
  const [done, setDone] = React.useState(false);
  const [served, setServed] = React.useState<Record<number, ServedTurn>>({});
  const [model, setModel] = React.useState<string | null>(() => readPref("composerModel"));
  const [reasoningEffort, setReasoningEffort] = React.useState<string | null>(() =>
    liveEffort(readPref("composerEffort")),
  );
  const [fastMode, setFastMode] = React.useState(false);
  const [dirty, setDirty] = React.useState<ReadonlySet<TaskInterviewField>>(() => new Set());

  const abortRef = React.useRef<AbortController | null>(null);
  // The latest task, read when a turn starts and ends: a turn outlives the
  // render that started it, and the user may edit fields meanwhile.
  const taskRef = React.useRef(task);
  const dirtyRef = React.useRef(dirty);
  React.useEffect(() => {
    taskRef.current = task;
    dirtyRef.current = dirty;
  });

  const clearStream = React.useCallback(() => {
    setBusy(false);
    setStreamText("");
    setThinking((prev) =>
      prev ? { ...prev, streaming: false, endedAt: prev.endedAt ?? Date.now() } : prev,
    );
  }, []);

  React.useEffect(() => {
    const onLocaleReload = () => {
      abortRef.current?.abort();
      abortRef.current = null;
      clearStream();
      setThinking(null);
      setOptions([]);
    };
    window.addEventListener(LOCALE_RELOAD_EVENT, onLocaleReload);
    return () => window.removeEventListener(LOCALE_RELOAD_EVENT, onLocaleReload);
  }, [clearStream]);

  React.useEffect(() => () => abortRef.current?.abort(), []);

  const runTurn = React.useCallback(
    (next: UnifiedChatTurn[]) => {
      // The controller is the synchronous in-flight guard: `busy` lags a render.
      if (abortRef.current) return;
      const current = taskRef.current;
      patchTask({ transcript: next });
      setBusy(true);
      setError(false);
      setErrorMessage(null);
      setDone(false);
      setStreamText("");
      setThinking(null);
      setOptions([]);
      const controller = new AbortController();
      abortRef.current = controller;

      void localAgentModel(model, agentSubscriptionFor("interview"))
        .then((sentModel) =>
          streamTaskInterviewTurn(
            {
              turns: next,
              objective: current.objective,
              background: current.background,
              artifact_kind: current.artifactKind,
              starting_version: current.startingVersion,
              locale: getActiveLocale(),
              model: sentModel ?? undefined,
              reasoning_effort: localAgentEffort(model, reasoningEffort, sentModel) ?? undefined,
              ...(sentFastMode(fastMode, sentModel) ? { fast_mode: true } : {}),
            },
            {
              onReasoning: (chunk: string) =>
                setThinking((prev) => ({
                  reasoning: (prev?.reasoning ?? "") + chunk,
                  startedAt: prev?.startedAt ?? Date.now(),
                  endedAt: null,
                  streaming: true,
                })),
              onMessagePatch: (chunk: string) => {
                setThinking((prev) =>
                  prev && prev.streaming
                    ? { ...prev, streaming: false, endedAt: Date.now() }
                    : prev,
                );
                setStreamText((text) => text + chunk);
              },
              onReset: () => {
                setStreamText("");
                setThinking(null);
              },
              onDone: (result: TaskInterviewTurnResult) => {
                const transcript: UnifiedChatTurn[] = [
                  ...next,
                  { role: "assistant", content: result.message },
                ];
                patchTask({ ...taskInterviewPatch(result, dirtyRef.current), transcript });
                setServed((prev) => ({
                  ...prev,
                  [transcript.length - 1]: {
                    model: result.model ?? null,
                    servedModel: result.served_model ?? null,
                  },
                }));
                setOptions(result.done ? [] : result.options);
                setDone(result.done);
                setBusy(false);
                setStreamText("");
                setThinking(null);
              },
              onError: (_message, code) => {
                setError(true);
                setErrorMessage(localModelRefusalMessage(code));
                clearStream();
              },
            },
            controller.signal,
          ),
        )
        .finally(() => {
          if (abortRef.current === controller) abortRef.current = null;
        });
    },
    [patchTask, model, reasoningEffort, fastMode, clearStream],
  );

  // The opening question fires once, while the transcript is empty; a
  // restart re-arms it. A cloned run opens too: the agent sees the filled
  // draft and asks what to change instead of starting from nothing.
  const { rearm } = useAgentKickoff(
    enabled && turns.length === 0 ? "task-interview-opening" : null,
    React.useCallback(() => runTurn([]), [runTurn]),
  );

  const send = React.useCallback(
    (content: string) => {
      if (!fitsInterview(content)) return false;
      runTurn([...turns, { role: "user", content }]);
      return true;
    },
    [runTurn, turns],
  );

  const editAndResend = React.useCallback(
    (messageIndex: number, content: string) => {
      if (!fitsInterview(content)) return false;
      runTurn([...turns.slice(0, messageIndex), { role: "user", content }]);
      return true;
    },
    [runTurn, turns],
  );

  const stop = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    clearStream();
  }, [clearStream]);

  // A transcript ending on the user's turn is unanswered (a stop, an error,
  // or a reload mid-turn), so retrying resends it as is.
  const retry = React.useCallback(() => {
    const last = turns[turns.length - 1];
    runTurn(last?.role === "assistant" ? turns.slice(0, -1) : turns);
  }, [runTurn, turns]);

  const restart = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    clearStream();
    setThinking(null);
    setOptions([]);
    setError(false);
    setErrorMessage(null);
    setDone(false);
    setServed({});
    patchTask({ transcript: [] });
    rearm();
  }, [clearStream, patchTask, rearm]);

  const editField = React.useCallback(
    <K extends TaskInterviewField>(field: K, value: TaskSlice[K]) => {
      setDirty((prev) => (prev.has(field) ? prev : new Set(prev).add(field)));
      // A hand-picked kind outdates the interview's reason for its own pick.
      patchTask(
        (field === "artifactKind"
          ? { artifactKind: value, artifactRationale: "" }
          : { [field]: value }) as Partial<TaskSlice>,
      );
    },
    [patchTask],
  );

  const isDirty = React.useCallback((field: TaskInterviewField) => dirty.has(field), [dirty]);

  const messages: AgentMessage[] = React.useMemo(() => {
    const mapped: AgentMessage[] = turns.map((t, i) => ({
      role: t.role,
      content: t.content,
      model: served[i]?.model ?? null,
      servedModel: served[i]?.servedModel ?? null,
    }));
    if (busy && (streamText || thinking)) mapped.push({ role: "assistant", content: streamText });
    return mapped;
  }, [turns, served, busy, streamText, thinking]);

  return {
    messages,
    busy,
    streamText,
    thinking,
    options,
    error,
    errorMessage,
    done,
    model,
    setModel,
    reasoningEffort,
    setReasoningEffort,
    fastMode,
    setFastMode,
    send,
    editAndResend,
    stop,
    retry,
    restart,
    editField,
    isDirty,
  };
}
