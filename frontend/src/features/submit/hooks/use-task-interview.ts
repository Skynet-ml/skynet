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
import {
  githubRepoFile,
  githubRepoTree,
  streamTaskInterviewTurn,
  type GithubRepository,
} from "@/shared/lib/api";
import { LOCALE_RELOAD_EVENT } from "@/shared/lib/locale";
import { liveEffort, sentFastMode } from "@/shared/lib/effort-slider";
import { msg } from "@/shared/lib/messages";
import { getActiveLocale } from "@/shared/lib/runtime-locale";
import type {
  InterviewOption,
  TaskInterviewRepoContext,
  TaskInterviewTurnResult,
  UnifiedChatTurn,
} from "@/shared/types/unified-run";
import type { AgentMessage, AgentThinking } from "@/shared/ui/agent";

import { fitsInterviewTurn, INTERVIEW_TURN_MAX_CHARS } from "../lib/interview-turn";
import { readmePath, repoInterviewContext } from "../lib/repo-interview";
import {
  REPO_NAME_PATTERN,
  emptyRepoDraft,
  programTargetOptimizer,
  repoTargetSlices,
  taskInterviewPatch,
  type TaskSlice,
  type WizardV2Context,
} from "../lib/wizard-v2";

/** Task fields the interview writes and the user may take over by editing. */
export type TaskInterviewField =
  | "objective"
  | "background"
  | "artifactKind"
  | "startingVersion"
  | "repo";

/** The repository a pick names: its full name and the branch to start from. */
export type PickedRepo = Pick<GithubRepository, "full_name" | "default_branch">;

// Long enough that a name typed by hand is read once it is complete.
const REPO_CONTEXT_DELAY_MS = 400;

/** Read a repository's README and file tree for the interview, cut to its caps. */
async function loadRepoContext(repository: string, ref: string): Promise<TaskInterviewRepoContext> {
  const tree = await githubRepoTree(repository, ref);
  const path = readmePath(tree.entries);
  const readme = path ? await githubRepoFile(repository, ref, path).catch(() => null) : null;
  return repoInterviewContext(repository, readme?.text ?? "", tree.entries, tree.truncated);
}

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
  /** The repository the user named in the chat; empty when none. */
  repoQuery: string;
  /** Choose a repository from the chat; the interview then reads it and replies. */
  pickRepo: (repo: PickedRepo) => void;
  /** Switch the target by hand; the interview never switches it back afterwards. */
  setTarget: (target: "dspy_program" | "repo") => void;
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
  const { state, patchTask, patchInputs, patchOptimizer } = ctx;
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
  const [repoContext, setRepoContext] = React.useState<TaskInterviewRepoContext | null>(null);
  // Counts finished turns, so a repository turn that waited on one runs next.
  const [turnsEnded, setTurnsEnded] = React.useState(0);

  const abortRef = React.useRef<AbortController | null>(null);
  // The latest task, read when a turn starts and ends: a turn outlives the
  // render that started it, and the user may edit fields meanwhile.
  const taskRef = React.useRef(task);
  const dirtyRef = React.useRef(dirty);
  const choiceRef = React.useRef(state.optimizer.choice);
  const repoContextRef = React.useRef(repoContext);
  React.useEffect(() => {
    taskRef.current = task;
    dirtyRef.current = dirty;
    choiceRef.current = state.optimizer.choice;
    repoContextRef.current = repoContext;
  });

  const switchToRepo = React.useCallback(() => {
    const { inputs, optimizer } = repoTargetSlices(choiceRef.current);
    patchInputs(inputs);
    if (optimizer) patchOptimizer(optimizer);
  }, [patchInputs, patchOptimizer]);

  const switchToProgram = React.useCallback(() => {
    const optimizer = programTargetOptimizer(choiceRef.current);
    if (optimizer) patchOptimizer(optimizer);
  }, [patchOptimizer]);

  // A repository chosen after the chat started gets one turn of its own, so
  // the agent restates the goal from its README and file tree. A pick from
  // the chat forces that turn even when the repository cannot be read; a
  // name typed by hand only gets it once the repository reads.
  const pendingTurnRef = React.useRef<{ repository: string; force: boolean } | null>(null);

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
    (next: UnifiedChatTurn[], context?: TaskInterviewRepoContext | null) => {
      // The controller is the synchronous in-flight guard: `busy` lags a render.
      if (abortRef.current) return false;
      const current = taskRef.current;
      const repoCtx = context === undefined ? repoContextRef.current : context;
      const chosen = current.repo?.repository.trim() ?? "";
      const sentContext = chosen && repoCtx?.repository === chosen ? repoCtx : null;
      // A turn that already carries the pending repository's context answers it.
      if (sentContext && pendingTurnRef.current?.repository === chosen)
        pendingTurnRef.current = null;
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
              target: current.repo ? "repo" : null,
              repo_context: sentContext,
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
                const patch = taskInterviewPatch(result, dirtyRef.current, taskRef.current);
                patchTask({ ...patch, transcript });
                if (patch.repo) switchToRepo();
                else if (patch.repo === null) switchToProgram();
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
          if (abortRef.current !== controller) return;
          abortRef.current = null;
          // A repository picked while this turn ran still gets its own turn.
          if (!controller.signal.aborted) setTurnsEnded((n) => n + 1);
        });
      return true;
    },
    [patchTask, switchToRepo, switchToProgram, model, reasoningEffort, fastMode, clearStream],
  );
  const runTurnRef = React.useRef(runTurn);
  React.useEffect(() => {
    runTurnRef.current = runTurn;
  });

  const settledRef = React.useRef<{
    repository: string;
    context: TaskInterviewRepoContext | null;
  } | null>(null);
  const runPending = React.useCallback(() => {
    const pending = pendingTurnRef.current;
    const settled = settledRef.current;
    if (!pending || settled?.repository !== pending.repository) return;
    if (!(settled.context || pending.force)) return;
    const started = runTurnRef.current(
      [...taskRef.current.transcript, { role: "user", content: pending.repository }],
      settled.context,
    );
    if (started) pendingTurnRef.current = null;
  }, []);
  React.useEffect(() => {
    if (turnsEnded > 0) runPending();
  }, [turnsEnded, runPending]);
  const repository = task.repo?.repository.trim() ?? "";
  const repoRef = task.repo ? task.repo.commit.trim() || task.repo.branch.trim() : "";
  const validRepo = REPO_NAME_PATTERN.test(repository);
  const seenRepoRef = React.useRef(repository);
  React.useEffect(() => {
    if (repository === seenRepoRef.current) return;
    seenRepoRef.current = repository;
    const pending = pendingTurnRef.current;
    if (validRepo && taskRef.current.transcript.length > 0 && pending?.repository !== repository)
      pendingTurnRef.current = { repository, force: false };
  }, [repository, validRepo]);

  React.useEffect(() => {
    setRepoContext(null);
    settledRef.current = null;
    if (!enabled || !validRepo) return;
    let cancelled = false;
    const settle = (context: TaskInterviewRepoContext | null) => {
      if (cancelled) return;
      setRepoContext(context);
      // A turn may read the context before the render that stores it.
      repoContextRef.current = context;
      settledRef.current = { repository, context };
      runPending();
    };
    const timer = window.setTimeout(() => {
      loadRepoContext(repository, repoRef).then(settle, () => settle(null));
    }, REPO_CONTEXT_DELAY_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [enabled, validRepo, repository, repoRef, runPending]);

  const pickRepo = React.useCallback(
    (picked: PickedRepo) => {
      pendingTurnRef.current = { repository: picked.full_name, force: true };
      const current = taskRef.current.repo;
      patchTask({
        repo: {
          ...(current ?? emptyRepoDraft()),
          repository: picked.full_name,
          branch: picked.default_branch ?? "",
          commit: "",
        },
      });
      if (!current) switchToRepo();
    },
    [patchTask, switchToRepo],
  );

  const setTarget = React.useCallback(
    (target: "dspy_program" | "repo") => {
      setDirty((prev) => (prev.has("repo") ? prev : new Set(prev).add("repo")));
      if (target === "repo") {
        patchTask({ repo: taskRef.current.repo ?? emptyRepoDraft() });
        switchToRepo();
      } else {
        patchTask({ repo: null, artifactKind: "dspy_program" });
        switchToProgram();
      }
    },
    [patchTask, switchToRepo, switchToProgram],
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
    pendingTurnRef.current = null;
    // The interview's own target guess goes too; a chosen repository stays.
    const chosen = taskRef.current.repo?.repository.trim();
    const dropRepo = Boolean(taskRef.current.repo && !chosen);
    setDirty((prev) => {
      if (!prev.has("repo")) return prev;
      const next = new Set(prev);
      next.delete("repo");
      return next;
    });
    patchTask({
      transcript: [],
      repoQuery: "",
      needsData: undefined,
      ...(dropRepo ? { repo: null, artifactKind: "dspy_program" } : {}),
    });
    if (dropRepo) switchToProgram();
    rearm();
  }, [clearStream, patchTask, switchToProgram, rearm]);

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
    repoQuery: task.repoQuery ?? "",
    pickRepo,
    setTarget,
  };
}
