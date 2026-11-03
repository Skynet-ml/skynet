"use client";

import * as React from "react";
import { toast } from "react-toastify";
import {
  localAgentEffort,
  localAgentModel,
  localModelRefusalMessage,
  useLocalStatus,
} from "@/features/local-runtime";
import { agentSubscriptionFor, readPref } from "@/features/settings";
import { formatMsg, msg } from "@/shared/lib/messages";
import { I18N_KEY } from "@/shared/lib/i18n";
import { getActiveLocale } from "@/shared/lib/runtime-locale";
import { liveEffort, sentFastMode } from "@/shared/lib/effort-slider";
import {
  appendReasoning,
  closeThinking,
  lastEventAt,
  reopenThinking,
  startThinking,
  summarizeThinking,
  type ThinkingSegment,
} from "@/shared/lib/thinking-segments";

import type { AgentMessage, AgentStatus, AgentToolCall } from "@/shared/ui/agent/types";
import { parseTurnStats } from "@/shared/ui/agent/turn-stats";
import { midTurnOf, SteerQueue, type MidTurnQueue } from "@/shared/ui/agent/steer-queue";
import { agentSteerChannel, newSteerKey } from "@/shared/ui/agent/use-steer-queue";

import { confirmGeneralistApproval, streamGeneralistAgent } from "../lib/stream";
import { dropPendingToolCalls, pendingToolCall, placeToolCall } from "../lib/pending-tool-calls";
import type {
  ChatTurn,
  PendingApprovalPayload,
  ToolEndPayload,
  ToolStartPayload,
  TrustMode,
  WizardState,
} from "../lib/types";

/** Where a stream event came from, relative to what the panel is showing. */
export interface SessionEventContext {
  /** True when the event's session is the one currently displayed. */
  isActive: boolean;
  /** The session's conversation id at event time (null before meta arrives). */
  conversationId: string | null;
}

export interface GeneralistAgentState {
  status: AgentStatus;
  statusLabel: string;
  messages: AgentMessage[];
  error: string | null;
  /** Machine code of the error, e.g. the balance gate's, so the panel can offer its way out. */
  errorCode: string | null;
  pendingApproval: PendingApprovalPayload | null;
  conversationId: string | null;
  /** LiteLLM id of the composer menu's chosen model; null runs the default. */
  model: string | null;
  setModel: (model: string | null) => void;
  /** Reasoning-effort level for the chosen model; null runs its default. */
  reasoningEffort: string | null;
  setReasoningEffort: (effort: string | null) => void;
  /** Fast mode for the next turns on a local CLI model that takes it. */
  fastMode: boolean;
  setFastMode: (on: boolean) => void;
  /** Send a chat turn; ``hidden`` sends it without a user bubble (a kickoff the app started). */
  send: (
    message: string,
    wizardStateOverride?: WizardState,
    options?: { hidden?: boolean },
  ) => void;
  /** The displayed chat's messages sent mid-turn: steered into the running
   *  turn or queued to run after it. */
  midTurn: MidTurnQueue;
  editAndResend: (messageIndex: number, content: string) => void;
  retry: () => void;
  stop: () => void;
  confirmApproval: (approved: boolean) => Promise<void>;
  /** Start a fresh empty chat; every other chat keeps streaming untouched. */
  newSession: () => void;
  /**
   * Switch to the live session already holding this conversation, if one
   * exists (streaming, queued, or finished-but-kept). Returns false when the
   * conversation has no in-memory session and must be loaded from the server.
   */
  activateConversation: (id: string) => boolean;
  /** Open a server-loaded conversation as the displayed session. */
  openConversation: (id: string, messages: AgentMessage[]) => void;
  /** Drop a conversation's session (aborting its stream) after deletion. */
  discardConversation: (id: string) => void;
  /** Conversation ids with a stream running or waiting in the queue. */
  busyConversationIds: ReadonlySet<string>;
  /** Busy sessions other than the displayed one (badge / pill signal). */
  backgroundBusyCount: number;
}

export interface UseGeneralistAgentArgs {
  wizardState: WizardState;
  trustMode: TrustMode;
  onToolStart?: (ev: ToolStartPayload, ctx: SessionEventContext) => void;
  onToolEnd?: (ev: ToolEndPayload, ctx: SessionEventContext) => void;
  onConversationMeta?: (id: string, title: string, ctx: SessionEventContext) => void;
}

// MCP tool names that mutate the user's optimization set. When one of these
// completes successfully inside the generalist agent, dispatch the same
// window event that manual UI flows (DeleteJobDialog, bulk delete, etc.) fire,
// so the sidebar and dashboard refresh without a page reload.
const OPTIMIZATION_MUTATING_TOOLS: ReadonlySet<string> = new Set([
  "delete_job_optimizations",
  "bulk_delete_jobs_optimizations_bulk_delete_post",
  "cancel_job_optimizations",
  "bulk_cancel_jobs_optimizations_bulk_cancel_post",
  "submit_unified_run_optimize_post",
  "rename_job_optimizations",
  "toggle_pin_job_optimizations",
  "clone_job_optimizations",
  "retry_job_optimizations",
  "bulk_pin_jobs_optimizations_bulk_pin_post",
  "pause_job_optimizations",
  "resume_job_optimizations",
  "restart_job_optimizations",
]);

// Submit tools whose success consumes the staged dataset + readiness. After
// one fires we drop the sticky wizard overlay so the next turn starts clean
// rather than re-carrying the just-submitted run's dataset id and flags.
const SUBMIT_TOOLS: ReadonlySet<string> = new Set(["submit_unified_run_optimize_post"]);

// Cap on each prior tool result echoed back in chat history; the backend
// bounds it again, this just keeps the request body small.
const HISTORY_TOOL_RESULT_MAX_CHARS = 400;

function historyToolCalls(m: AgentMessage): ChatTurn["tool_calls"] {
  const calls = (m.toolCalls ?? []).filter((c) => c.tool !== "submit" && c.status !== "running");
  if (m.role !== "assistant" || calls.length === 0) return undefined;
  return calls.map((c) => {
    const result = c.payload?.result;
    return {
      tool: c.tool,
      status: c.status === "error" ? "error" : "done",
      ...(result === undefined
        ? {}
        : { result: JSON.stringify(result).slice(0, HISTORY_TOOL_RESULT_MAX_CHARS) }),
    };
  });
}

// SSE streams hold one HTTP connection each for their whole lifetime. Over
// HTTP/1.1 the browser allows six connections per origin, so an uncapped
// fan-out would starve every other API call to the backend. Runs beyond the
// cap wait in a FIFO queue and start automatically as slots free up.
const MAX_PARALLEL_STREAMS = 4;

// The first session's key; ``keyCounterRef`` therefore starts at 1.
const INITIAL_SESSION_KEY = "session-0";

/** Per-session render state — everything the panel needs to draw one chat. */
interface SessionView {
  key: string;
  conversationId: string | null;
  status: AgentStatus;
  statusLabel: string;
  messages: AgentMessage[];
  error: string | null;
  /** Machine code of the error, e.g. the balance gate's, so the panel can offer its way out. */
  errorCode: string | null;
  pendingApproval: PendingApprovalPayload | null;
}

/** A turn captured at send time, replayed verbatim when a slot frees up. */
interface PendingRun {
  message: string;
  chatHistory: ChatTurn[];
  wizardState: WizardState;
  trustMode: TrustMode;
  regenerate: boolean;
}

/** Per-session mutable machinery that must never trigger a render. */
interface SessionRuntime {
  controller: AbortController | null;
  replyBuf: string;
  // Sticky overlay of wizard fields derived synchronously this session
  // (e.g. ``staged_dataset_id`` from an in-panel upload). The panel-only
  // flow has no ``wizardCtx`` to write into, so without this every turn
  // after the upload would lose the staged dataset and the agent would
  // ask the user to re-upload.
  persistentExtras: Partial<WizardState>;
  pendingRun: PendingRun | null;
  /** Index of the running turn's first assistant message; a steer adds more after it. */
  turnStart: number;
}

function blankSession(key: string): SessionView {
  return {
    key,
    conversationId: null,
    status: "idle",
    statusLabel: "",
    messages: [],
    error: null,
    errorCode: null,
    pendingApproval: null,
  };
}

function blankRuntime(): SessionRuntime {
  return {
    controller: null,
    replyBuf: "",
    persistentExtras: {},
    pendingRun: null,
    turnStart: 0,
  };
}

function isBusy(session: SessionView): boolean {
  return session.status === "streaming" || session.status === "queued";
}

/**
 * Multi-session generalist-agent manager. Every chat owns its own stream,
 * buffers, and abort controller, so switching threads or opening a new chat
 * never cancels work in progress — streams keep running in the background
 * and their state is re-attached when the user returns. Up to
 * ``MAX_PARALLEL_STREAMS`` turns run concurrently; further sends wait in a
 * FIFO queue. The returned state mirrors the previously single-session shape,
 * projected from whichever session is currently displayed.
 */
export function useGeneralistAgent(args: UseGeneralistAgentArgs): GeneralistAgentState {
  const { wizardState, trustMode } = args;

  const keyCounterRef = React.useRef(1);
  const nextKey = React.useCallback(() => `session-${keyCounterRef.current++}`, []);

  const [sessions, setSessions] = React.useState<ReadonlyMap<string, SessionView>>(
    () => new Map([[INITIAL_SESSION_KEY, blankSession(INITIAL_SESSION_KEY)]]),
  );
  const sessionsRef = React.useRef(sessions);
  const [activeKey, setActiveKey] = React.useState(INITIAL_SESSION_KEY);
  const activeKeyRef = React.useRef(activeKey);

  const runtimesRef = React.useRef(new Map<string, SessionRuntime>());
  const streamingKeysRef = React.useRef(new Set<string>());
  const queueRef = React.useRef<string[]>([]);

  // Each chat's follow-ups sent mid-turn. Separate from the parallel-stream
  // queue above: these wait for their own chat's turn to end, then run in it.
  // Each queue's sender is attached by the effect further down.
  const [steerQueues] = React.useState(() => new Map<string, SteerQueue>());
  // Loads the CLI list a role set to run locally maps its model onto.
  useLocalStatus();
  const steerQueueFor = React.useCallback(
    (key: string): SteerQueue => {
      let queue = steerQueues.get(key);
      if (!queue) {
        queue = new SteerQueue(undefined, 20_000);
        steerQueues.set(key, queue);
      }
      return queue;
    },
    [steerQueues],
  );
  const dropSteerQueue = React.useCallback(
    (key: string) => {
      steerQueues.get(key)?.clear();
      steerQueues.delete(key);
    },
    [steerQueues],
  );

  // Seeded from the settings-modal default; the panel is client-only
  // (ssr:false), so the localStorage read is hydration-safe.
  const [model, setModelState] = React.useState<string | null>(() => readPref("composerModel"));
  // Mirrored in a ref so the streaming closures (retry, regenerate) always
  // send the current choice without re-memoizing the run machinery.
  const modelRef = React.useRef<string | null>(model);
  const setModel = React.useCallback((next: string | null) => {
    modelRef.current = next;
    setModelState(next);
  }, []);
  const [reasoningEffort, setReasoningEffortState] = React.useState<string | null>(() =>
    liveEffort(readPref("composerEffort")),
  );
  const effortRef = React.useRef<string | null>(reasoningEffort);
  const setReasoningEffort = React.useCallback((next: string | null) => {
    effortRef.current = next;
    setReasoningEffortState(next);
  }, []);
  const [fastMode, setFastModeState] = React.useState(false);
  const fastRef = React.useRef(false);
  const setFastMode = React.useCallback((next: boolean) => {
    fastRef.current = next;
    setFastModeState(next);
  }, []);

  // Effect ordering guarantees this snapshot is current at send time: this
  // child hook's effects run before the parent panel's, so any same-commit
  // programmatic send (e.g. the code-authoring handoff) sees the latest props.
  // Same-tick sends that must carry not-yet-committed values pass them via the
  // ``wizardStateOverride`` argument instead (see the dataset/code handoffs).
  const snapshotRef = React.useRef({ wizardState, trustMode });
  React.useEffect(() => {
    snapshotRef.current = { wizardState, trustMode };
  }, [wizardState, trustMode]);

  const callbacksRef = React.useRef<{
    onToolStart: UseGeneralistAgentArgs["onToolStart"];
    onToolEnd: UseGeneralistAgentArgs["onToolEnd"];
    onConversationMeta: UseGeneralistAgentArgs["onConversationMeta"];
  }>({
    onToolStart: args.onToolStart,
    onToolEnd: args.onToolEnd,
    onConversationMeta: args.onConversationMeta,
  });
  React.useEffect(() => {
    callbacksRef.current = {
      onToolStart: args.onToolStart,
      onToolEnd: args.onToolEnd,
      onConversationMeta: args.onConversationMeta,
    };
  }, [args.onToolStart, args.onToolEnd, args.onConversationMeta]);

  // The ref is updated synchronously with every commit so stream callbacks and
  // same-tick sends always read post-mutation state, never a stale render.
  const commit = React.useCallback((mutate: (draft: Map<string, SessionView>) => void) => {
    const next = new Map(sessionsRef.current);
    mutate(next);
    sessionsRef.current = next;
    setSessions(next);
  }, []);

  const patchSession = React.useCallback(
    (key: string, patch: Partial<SessionView>) => {
      commit((draft) => {
        const cur = draft.get(key);
        if (cur) draft.set(key, { ...cur, ...patch });
      });
    },
    [commit],
  );

  const patchMessages = React.useCallback(
    (key: string, updater: (prev: AgentMessage[]) => AgentMessage[]) => {
      commit((draft) => {
        const cur = draft.get(key);
        if (!cur) return;
        const nextMessages = updater(cur.messages);
        if (nextMessages !== cur.messages) draft.set(key, { ...cur, messages: nextMessages });
      });
    },
    [commit],
  );

  const getRuntime = React.useCallback((key: string): SessionRuntime => {
    let rt = runtimesRef.current.get(key);
    if (!rt) {
      rt = blankRuntime();
      runtimesRef.current.set(key, rt);
    }
    return rt;
  }, []);

  const setActive = React.useCallback((key: string) => {
    activeKeyRef.current = key;
    setActiveKey(key);
  }, []);

  const appendReply = React.useCallback(
    (key: string, chunk: string) => {
      patchMessages(key, (prev) => {
        const last = prev[prev.length - 1];
        if (!last || last.role !== "assistant") return prev;
        const next = prev.slice();
        next[next.length - 1] = { ...last, content: last.content + chunk };
        return next;
      });
    },
    [patchMessages],
  );

  const pushToolCall = React.useCallback(
    (key: string, call: AgentToolCall) => {
      patchMessages(key, (prev) => {
        const last = prev[prev.length - 1];
        if (!last || last.role !== "assistant") return prev;
        const next = prev.slice();
        next[next.length - 1] = { ...last, toolCalls: placeToolCall(last.toolCalls ?? [], call) };
        return next;
      });
    },
    [patchMessages],
  );

  // The turn's thinking lives on its newest assistant message, so a steer's
  // fresh bubble starts its own segments.
  const patchThinking = React.useCallback(
    (
      key: string,
      update: (
        segments: readonly ThinkingSegment[],
        toolCount: number,
        since: number | null,
      ) => readonly ThinkingSegment[],
    ) => {
      patchMessages(key, (prev) => {
        const last = prev[prev.length - 1];
        if (!last || last.role !== "assistant") return prev;
        const segments = last.thinkingSegments ?? [];
        const nextSegments = update(
          segments,
          last.toolCalls?.length ?? 0,
          lastEventAt(segments, last.toolCalls ?? []),
        );
        if (nextSegments === segments) return prev;
        const next = prev.slice();
        next[next.length - 1] = { ...last, thinkingSegments: nextSegments };
        return next;
      });
    },
    [patchMessages],
  );

  const finishToolCall = React.useCallback(
    (key: string, id: string, nextStatus: "done" | "error", result?: unknown) => {
      patchMessages(key, (prev) => {
        const last = prev[prev.length - 1];
        if (!last || last.role !== "assistant" || !last.toolCalls?.length) return prev;
        const next = prev.slice();
        next[next.length - 1] = {
          ...last,
          toolCalls: last.toolCalls.map((t) =>
            t.id === id
              ? {
                  ...t,
                  status: nextStatus,
                  endedAt: Date.now(),
                  payload: { ...(t.payload ?? {}), result },
                }
              : t,
          ),
        };
        return next;
      });
    },
    [patchMessages],
  );

  const eventContext = React.useCallback((key: string): SessionEventContext => {
    return {
      isActive: activeKeyRef.current === key,
      conversationId: sessionsRef.current.get(key)?.conversationId ?? null,
    };
  }, []);

  // ``pumpQueue`` (below) and ``startStream`` are mutually recursive: a
  // finished stream frees a slot, which starts the next queued stream. The
  // back-edge goes through a state tick — releasing a slot bumps the tick and
  // the pump effect runs on the next commit, so neither callback needs the
  // other in scope at declaration time.
  const [pumpTick, setPumpTick] = React.useState(0);
  const requestPump = React.useCallback(() => setPumpTick((t) => t + 1), []);

  const startStream = React.useCallback(
    (key: string, run: PendingRun) => {
      const rt = getRuntime(key);
      const controller = new AbortController();
      rt.controller = controller;
      rt.replyBuf = "";
      rt.turnStart = Math.max(0, (sessionsRef.current.get(key)?.messages.length ?? 1) - 1);
      // Labels can land after `done`, when the next turn may have moved rt on.
      const turnStart = rt.turnStart;
      let steerAnchor = sessionsRef.current.get(key)?.messages[turnStart - 1];
      // The reply `done` landed on; stats that arrive after it belong there.
      let replyAt: number | null = null;
      let pendingSeq = 0;
      // Rows for calls the model wrote but never ran (a failed parse, a
      // denied step) must not outlive the turn as spinning pills.
      const dropPendingRows = () =>
        patchMessages(key, (prev) => {
          const last = prev[prev.length - 1];
          if (!last || last.role !== "assistant" || !last.toolCalls?.length) return prev;
          const kept = dropPendingToolCalls(last.toolCalls);
          if (kept.length === last.toolCalls.length) return prev;
          const next = prev.slice();
          next[next.length - 1] = { ...last, toolCalls: kept };
          return next;
        });
      streamingKeysRef.current.add(key);

      patchSession(key, {
        status: "streaming",
        statusLabel: msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.1"),
        error: null,
        errorCode: null,
        pendingApproval: null,
      });
      patchThinking(key, (segments, toolCount) => startThinking(segments, Date.now(), toolCount));

      const releaseSlot = () => {
        rt.controller = null;
        if (streamingKeysRef.current.delete(key)) requestPump();
      };

      const steerKey = newSteerKey();
      const steerQueue = steerQueueFor(key);
      const applySteer = ({ ids, text }: { ids: string[]; text: string }) => {
        if (!steerQueue.applied(ids)) return;
        if (!text.trim()) return;
        const message: AgentMessage = { role: "user", content: text, createdAt: Date.now() };
        const previousAnchor = steerAnchor;
        steerAnchor = message;
        if (rt.controller !== controller) {
          patchMessages(key, (prev) => {
            const index = previousAnchor ? prev.indexOf(previousAnchor) : -1;
            if (index < 0) return prev;
            let end = index + 1;
            while (end < prev.length && prev[end]?.role !== "user") end++;
            return [...prev.slice(0, end), message, ...prev.slice(end)];
          });
          return;
        }
        dropPendingRows();
        replyBeforeSteer = (replyBeforeSteer ?? "") + rt.replyBuf;
        rt.replyBuf = "";
        patchThinking(key, (segments) => closeThinking(segments, Date.now()));
        patchMessages(key, (prev) => {
          const last = prev[prev.length - 1];
          const kept = last && last.role === "assistant" && !last.content &&
            !last.toolCalls?.length && !last.thinkingSegments?.length && !last.plan
            ? prev.slice(0, -1) : prev;
          return [...kept, message,
            { role: "assistant", content: "", toolCalls: [] }];
        });
      };
      steerQueue.openTurn(agentSteerChannel(steerKey, applySteer));
      // The reply bubble closed by the latest applied steer, which the
      // finished turn's `assistant_message` may repeat.
      let replyBeforeSteer: string | null = null;

      // Every stream callback short-circuits on ``controller.signal.aborted``
      // so a slow stream replaced by a fresh ``send`` (or by the unmount-time
      // abort) cannot leak state writes or window events into the live view.
      void localAgentModel(modelRef.current, agentSubscriptionFor("generalist")).then((sentModel) =>
        streamGeneralistAgent(
          {
            user_message: run.message,
            chat_history: run.chatHistory,
            wizard_state: run.wizardState,
            trust_mode: run.trustMode,
            conversation_id: sessionsRef.current.get(key)?.conversationId ?? null,
            regenerate: run.regenerate,
            locale: getActiveLocale(),
            model: sentModel ?? undefined,
            reasoning_effort:
              localAgentEffort(modelRef.current, effortRef.current, sentModel) ?? undefined,
            fast_mode: sentFastMode(fastRef.current, sentModel) || undefined,
            steer_key: steerKey,
          },
          {
            signal: controller.signal,
            onSteerApplied: ({ ids, text }) => {
              if (controller.signal.aborted) return;
              applySteer({ ids, text });
            },
            onConversationMeta: (ev) => {
              if (controller.signal.aborted) return;
              if (!ev.conversation_id) return;
              patchSession(key, { conversationId: ev.conversation_id });
              callbacksRef.current.onConversationMeta?.(
                ev.conversation_id,
                ev.title,
                eventContext(key),
              );
            },
            onReasoningPatch: (chunk) => {
              if (controller.signal.aborted) return;
              patchThinking(key, (segments, toolCount, since) =>
                appendReasoning(segments, chunk, Date.now(), toolCount, since),
              );
            },
            onThinkingSummary: ({ index, text }) => {
              if (controller.signal.aborted) return;
              patchMessages(key, (prev) => summarizeThinking(prev, turnStart, index, text));
            },
            onPlan: (text) => {
              if (controller.signal.aborted) return;
              patchMessages(key, (prev) => {
                const last = prev[prev.length - 1];
                if (!last || last.role !== "assistant") return prev;
                const next = prev.slice();
                next[next.length - 1] = { ...last, plan: text };
                return next;
              });
            },
            onToolPending: ({ tool }) => {
              if (controller.signal.aborted) return;
              patchThinking(key, (segments) => closeThinking(segments, Date.now()));
              patchSession(key, {
                statusLabel: formatMsg(
                  "auto.features.agent.panel.hooks.use.generalist.agent.template.1",
                  { p1: tool },
                ),
              });
              pushToolCall(key, pendingToolCall(tool, pendingSeq++, Date.now()));
            },
            onToolPendingClear: () => {
              if (controller.signal.aborted) return;
              dropPendingRows();
            },
            onToolStart: (ev) => {
              if (controller.signal.aborted) return;
              // A tool call ends the current stretch of thinking; reasoning
              // after it opens a new "Thought for" row below the tool.
              patchThinking(key, (segments) => closeThinking(segments, Date.now()));
              patchSession(key, {
                statusLabel: formatMsg(
                  "auto.features.agent.panel.hooks.use.generalist.agent.template.1",
                  { p1: ev.tool },
                ),
              });
              pushToolCall(key, {
                id: ev.id,
                tool: ev.tool,
                reason: ev.reason,
                status: "running",
                startedAt: Date.now(),
                endedAt: null,
                payload: { arguments: ev.arguments },
              });
              callbacksRef.current.onToolStart?.(ev, eventContext(key));
            },
            onToolEnd: (ev) => {
              if (controller.signal.aborted) return;
              finishToolCall(key, ev.id, ev.status === "ok" ? "done" : "error", ev.result);
              patchSession(key, {
                statusLabel: msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.1"),
              });
              if (ev.status === "ok") {
                if (OPTIMIZATION_MUTATING_TOOLS.has(ev.tool)) {
                  window.dispatchEvent(new Event("optimizations-changed"));
                }
                if (SUBMIT_TOOLS.has(ev.tool)) {
                  rt.persistentExtras = {};
                }
              }
              callbacksRef.current.onToolEnd?.(ev, eventContext(key));
            },
            onPendingApproval: (ev) => {
              if (controller.signal.aborted) return;
              patchSession(key, {
                pendingApproval: ev,
                statusLabel: msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.2"),
              });
            },
            onApprovalResolved: () => {
              if (controller.signal.aborted) return;
              patchSession(key, {
                pendingApproval: null,
                statusLabel: msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.3"),
              });
            },
            onMessagePatch: (chunk) => {
              if (controller.signal.aborted) return;
              if (rt.replyBuf === "") {
                patchSession(key, {
                  statusLabel: msg(
                    "auto.features.agent.panel.hooks.use.generalist.agent.literal.4",
                  ),
                });
                patchThinking(key, (segments) => closeThinking(segments, Date.now()));
              }
              rt.replyBuf += chunk;
              appendReply(key, chunk);
            },
            onMessageReset: () => {
              if (controller.signal.aborted) return;
              rt.replyBuf = "";
              patchThinking(key, reopenThinking);
              patchMessages(key, (prev) => {
                const last = prev[prev.length - 1];
                if (!last || last.role !== "assistant" || last.content === "") return prev;
                const next = prev.slice();
                next[next.length - 1] = { ...last, content: "" };
                return next;
              });
            },
            onDone: (result) => {
              if (controller.signal.aborted) return;
              dropPendingRows();
              patchSession(key, {
                status: "done",
                statusLabel: "",
              });
              patchThinking(key, (segments) => closeThinking(segments, Date.now()));
              patchMessages(key, (prev) => {
                const last = prev[prev.length - 1];
                if (!last || last.role !== "assistant") return prev;
                let reply = result.assistant_message;
                if (replyBeforeSteer !== null) {
                  if (replyBeforeSteer && reply.startsWith(replyBeforeSteer)) {
                    reply = reply.slice(replyBeforeSteer.length).trimStart();
                  }
                  reply = last.content || reply;
                  // Nothing came after the steer: no hollow bubble.
                  if (!reply && !last.toolCalls?.length && !last.thinkingSegments?.length) {
                    return prev.slice(0, -1);
                  }
                }
                const fallback =
                  last.content ||
                  (last.toolCalls?.length
                    ? ""
                    : msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.5"));
                const next = prev.slice();
                replyAt = next.length - 1;
                next[replyAt] = {
                  ...last,
                  content: reply || fallback,
                  model: result.model,
                  servedModel: result.served_model,
                  stats: parseTurnStats(result.stats),
                  createdAt: Date.now(),
                };
                return next;
              });
              releaseSlot();
            },
            onTurnStats: (raw) => {
              if (controller.signal.aborted) return;
              const stats = parseTurnStats(raw);
              if (!stats) return;
              patchMessages(key, (prev) => {
                const at = replyAt;
                const reply = at === null ? undefined : prev[at];
                if (at === null || !reply || reply.role !== "assistant") return prev;
                const next = prev.slice();
                next[at] = { ...reply, stats };
                return next;
              });
            },
            onError: (message, code) => {
              if (controller.signal.aborted) return;
              dropPendingRows();
              patchSession(key, {
                status: "error",
                statusLabel: msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.6"),
                // Known machine codes get a localized message; anything else
                // shows the backend's text as-is.
                error:
                  localModelRefusalMessage(code) ??
                  (code === "context_too_long"
                    ? msg("agent.error.context_too_long")
                    : code === I18N_KEY.BILLING_INSUFFICIENT_FUNDS
                      ? msg("agent.error.insufficient_funds")
                      : message),
                errorCode: code ?? null,
                // The stream that would act on the decision is gone.
                pendingApproval: null,
              });
              patchThinking(key, (segments) => closeThinking(segments, Date.now()));
              patchMessages(key, (prev) => {
                const last = prev[prev.length - 1];
                if (!last || last.role !== "assistant") return prev;
                if (!last.content && !last.toolCalls?.length && !last.thinkingSegments?.length) {
                  // Nothing rendered yet — drop the empty assistant placeholder.
                  return prev.slice(0, -1);
                }
                // Mark any in-flight tool pills as errored so they stop
                // spinning forever when the SSE socket dies mid-tool (e.g.
                // backend restart, network drop). Without this the chip
                // sits at "פועל כעת" indefinitely with no recovery path.
                const hasRunning = last.toolCalls?.some((t) => t.status === "running");
                if (!hasRunning) return prev;
                const next = prev.slice();
                next[next.length - 1] = {
                  ...last,
                  toolCalls: last.toolCalls?.map((t) =>
                    t.status === "running" ? { ...t, status: "error", endedAt: Date.now() } : t,
                  ),
                };
                return next;
              });
              releaseSlot();
            },
          },
        ),
      );
    },
    [
      appendReply,
      eventContext,
      finishToolCall,
      getRuntime,
      patchMessages,
      patchSession,
      patchThinking,
      pushToolCall,
      requestPump,
      steerQueueFor,
    ],
  );

  // The pump is idempotent, so the mount-time effect run and
  // identity-change re-runs are harmless no-ops.
  const pumpQueue = React.useCallback(() => {
    while (streamingKeysRef.current.size < MAX_PARALLEL_STREAMS && queueRef.current.length > 0) {
      const key = queueRef.current.shift();
      if (!key) continue;
      const rt = runtimesRef.current.get(key);
      const run = rt?.pendingRun;
      if (!rt || !run || !sessionsRef.current.has(key)) continue;
      rt.pendingRun = null;
      startStream(key, run);
    }
  }, [startStream]);
  React.useEffect(() => {
    pumpQueue();
  }, [pumpTick, pumpQueue]);

  /** Cancel a session's stream or queued turn and free its slot. */
  const abortSession = React.useCallback(
    (key: string) => {
      const rt = runtimesRef.current.get(key);
      if (rt) {
        rt.controller?.abort();
        rt.controller = null;
        rt.pendingRun = null;
      }
      queueRef.current = queueRef.current.filter((k) => k !== key);
      if (streamingKeysRef.current.delete(key)) requestPump();
    },
    [requestPump],
  );

  // Abort every in-flight stream when the hook unmounts so callbacks can't
  // resume firing into a torn-down React tree (setState-on-unmounted warnings,
  // window-event dispatch from a stale tool-end, etc.).
  React.useEffect(
    () => () => {
      for (const rt of runtimesRef.current.values()) {
        rt.controller?.abort();
        rt.controller = null;
        rt.pendingRun = null;
      }
      queueRef.current = [];
      streamingKeysRef.current.clear();
    },
    [],
  );

  const requestRun = React.useCallback(
    (
      key: string,
      userMessage: string,
      history: AgentMessage[],
      wizardStateOverride?: WizardState,
      regenerate = false,
      hidden = false,
    ) => {
      // A resend within the same session replaces its own in-flight or queued
      // turn — other sessions' streams are untouched.
      abortSession(key);
      const rt = getRuntime(key);
      rt.replyBuf = "";

      const chatHistory: ChatTurn[] = history
        .filter((m) => m.content.trim().length > 0)
        .map((m) => {
          const toolCalls = historyToolCalls(m);
          return {
            role: m.role,
            content: m.content,
            ...(toolCalls ? { tool_calls: toolCalls } : {}),
          };
        });
      // Merge order: snapshot (from wizardCtx if mounted) <- sticky extras
      // (accumulated across turns from panel-side derivations like
      // ``staged_dataset_id``) <- this-turn override. The sticky layer
      // is what survives between turns when there's no wizardCtx writer.
      const { wizardState: snapshotWs, trustMode: tm } = snapshotRef.current;
      const ws = {
        ...snapshotWs,
        ...rt.persistentExtras,
        ...(wizardStateOverride ?? {}),
      };
      const run: PendingRun = {
        message: userMessage,
        chatHistory,
        wizardState: ws,
        trustMode: tm,
        regenerate,
      };

      commit((draft) => {
        const cur = draft.get(key);
        if (!cur) return;
        draft.set(key, {
          ...cur,
          messages: [
            ...history,
            {
              role: "user",
              content: userMessage,
              createdAt: Date.now(),
              ...(hidden ? { hidden } : {}),
            },
            { role: "assistant", content: "", toolCalls: [] },
          ],
          error: null,
          errorCode: null,
          pendingApproval: null,
        });
      });

      if (streamingKeysRef.current.size < MAX_PARALLEL_STREAMS) {
        startStream(key, run);
      } else {
        rt.pendingRun = run;
        queueRef.current.push(key);
        patchSession(key, {
          status: "queued",
          statusLabel: msg("agent.parallel.queued"),
        });
      }
    },
    [abortSession, commit, getRuntime, patchSession, startStream],
  );

  const send = React.useCallback(
    (message: string, wizardStateOverride?: WizardState, options?: { hidden?: boolean }) => {
      const trimmed = message.trim();
      if (!trimmed) return;
      const key = activeKeyRef.current;
      if (wizardStateOverride) {
        const rt = getRuntime(key);
        rt.persistentExtras = {
          ...rt.persistentExtras,
          ...wizardStateOverride,
        };
      }
      const history = sessionsRef.current.get(key)?.messages ?? [];
      requestRun(key, trimmed, history, wizardStateOverride, false, options?.hidden ?? false);
    },
    [getRuntime, requestRun],
  );

  // A follow-up runs in the chat it was sent from, displayed or not. Ending
  // a turn (done, error, stop) settles its unread steers and lets the chat's
  // queue run its next follow-up.
  React.useEffect(() => {
    for (const [key, queue] of steerQueues) {
      queue.setSend((text) => {
        const session = sessionsRef.current.get(key);
        if (!session) return false;
        requestRun(key, text, session.messages);
        return true;
      });
      const session = sessions.get(key);
      queue.setBusy(session ? isBusy(session) : false, session?.status === "error");
    }
  }, [sessions, steerQueues, requestRun, activeKey]);

  const editAndResend = React.useCallback(
    (messageIndex: number, content: string) => {
      const trimmed = content.trim();
      if (!trimmed) return;
      const key = activeKeyRef.current;
      const truncated = (sessionsRef.current.get(key)?.messages ?? []).slice(0, messageIndex);
      steerQueueFor(key).pause();
      requestRun(key, trimmed, truncated);
    },
    [requestRun, steerQueueFor],
  );

  // Re-run the most recent user turn (used by the error-banner retry button
  // and by the end-of-conversation regenerate action). Truncates back to the
  // user message so we don't re-feed a failed assistant turn into history.
  const retry = React.useCallback(() => {
    const key = activeKeyRef.current;
    const current = sessionsRef.current.get(key)?.messages ?? [];
    let lastUserIndex = -1;
    for (let i = current.length - 1; i >= 0; i--) {
      if (current[i]?.role === "user") {
        lastUserIndex = i;
        break;
      }
    }
    if (lastUserIndex === -1) return;
    const lastUser = current[lastUserIndex];
    if (!lastUser) return;
    const truncated = current.slice(0, lastUserIndex);
    steerQueueFor(key).pause();
    requestRun(key, lastUser.content, truncated, undefined, true, lastUser.hidden ?? false);
  }, [requestRun, steerQueueFor]);

  const stopSession = React.useCallback((key: string) => {
    steerQueueFor(key).pause();
    const wasQueued = sessionsRef.current.get(key)?.status === "queued";
    abortSession(key);
    patchSession(key, { status: "idle", statusLabel: "" });
    patchThinking(key, (segments) => closeThinking(segments, Date.now()));
    if (wasQueued) {
      // Nothing streamed yet — drop the empty assistant placeholder so the
      // transcript doesn't keep a hollow bubble for a turn that never ran.
      patchMessages(key, (prev) => {
        const last = prev[prev.length - 1];
        if (last && last.role === "assistant" && !last.content && !last.toolCalls?.length) {
          return prev.slice(0, -1);
        }
        return prev;
      });
    }
  }, [abortSession, patchMessages, patchSession, patchThinking, steerQueueFor]);

  const stop = React.useCallback(() => stopSession(activeKeyRef.current), [stopSession]);

  /**
   * Drop background sessions that are safely re-openable: idle/done ones whose
   * transcript lives on the server (or that are empty). Busy and errored
   * sessions are kept so running work and unseen failures survive switching.
   */
  const pruneInactive = React.useCallback(
    (draft: Map<string, SessionView>, keepKey: string) => {
      for (const [key, session] of draft) {
        if (key === keepKey) continue;
        if (isBusy(session) || session.status === "error") continue;
        if (session.conversationId === null && session.messages.length > 0) continue;
        if (steerQueues.get(key)?.hasPending()) continue;
        draft.delete(key);
        runtimesRef.current.delete(key);
        dropSteerQueue(key);
      }
    },
    [dropSteerQueue, steerQueues],
  );

  const newSession = React.useCallback(() => {
    const cur = sessionsRef.current.get(activeKeyRef.current);
    if (cur && cur.status === "idle" && cur.messages.length === 0 && cur.conversationId === null) {
      return;
    }
    const key = nextKey();
    commit((draft) => {
      draft.set(key, blankSession(key));
      pruneInactive(draft, key);
    });
    setActive(key);
  }, [commit, nextKey, pruneInactive, setActive]);

  const activateConversation = React.useCallback(
    (id: string): boolean => {
      let found: string | null = null;
      for (const [key, session] of sessionsRef.current) {
        if (session.conversationId === id) {
          found = key;
          break;
        }
      }
      if (found === null) return false;
      const key = found;
      commit((draft) => pruneInactive(draft, key));
      setActive(key);
      return true;
    },
    [commit, pruneInactive, setActive],
  );

  const openConversation = React.useCallback(
    (id: string, loaded: AgentMessage[]) => {
      const key = nextKey();
      commit((draft) => {
        draft.set(key, { ...blankSession(key), conversationId: id, messages: loaded });
        pruneInactive(draft, key);
      });
      setActive(key);
    },
    [commit, nextKey, pruneInactive, setActive],
  );

  const discardConversation = React.useCallback(
    (id: string) => {
      let found: string | null = null;
      for (const [key, session] of sessionsRef.current) {
        if (session.conversationId === id) {
          found = key;
          break;
        }
      }
      if (found === null) return;
      const key = found;
      abortSession(key);
      runtimesRef.current.delete(key);
      dropSteerQueue(key);
      const wasActive = activeKeyRef.current === key;
      if (wasActive) {
        const freshKey = nextKey();
        commit((draft) => {
          draft.delete(key);
          draft.set(freshKey, blankSession(freshKey));
        });
        setActive(freshKey);
      } else {
        commit((draft) => {
          draft.delete(key);
        });
      }
    },
    [abortSession, commit, nextKey, setActive, dropSteerQueue],
  );

  const active = sessions.get(activeKey) ?? blankSession(activeKey);

  const activeSteerQueue = steerQueueFor(activeKey);
  const steerSnapshot = React.useSyncExternalStore(
    activeSteerQueue.subscribe,
    activeSteerQueue.getSnapshot,
    activeSteerQueue.getSnapshot,
  );
  const midTurn = React.useMemo(
    () => ({
      ...midTurnOf(() => activeSteerQueue, steerSnapshot),
      stopAndSendNow: () => {
        const key = activeKey;
        activeSteerQueue.stopAndSendNow(() => stopSession(key));
      },
    }),
    [activeKey, activeSteerQueue, steerSnapshot, stopSession],
  );

  const confirmApproval = React.useCallback(
    async (approved: boolean) => {
      const key = activeKeyRef.current;
      const pa = sessionsRef.current.get(key)?.pendingApproval;
      if (!pa) return;
      patchSession(key, { pendingApproval: null });
      const resolved = await confirmGeneralistApproval(pa.id, approved);
      if (!resolved) {
        // The confirm never reached the stream's process (network blip or a
        // different replica) — silently dropping it leaves the tool hanging
        // with no feedback. Restore the card so the user can retry.
        patchSession(key, { pendingApproval: pa });
        toast.error(msg("agent.approval.confirm_failed"));
      }
    },
    [patchSession],
  );

  const busyConversationIds = React.useMemo(() => {
    const out = new Set<string>();
    for (const session of sessions.values()) {
      if (isBusy(session) && session.conversationId) out.add(session.conversationId);
    }
    return out;
  }, [sessions]);

  const backgroundBusyCount = React.useMemo(() => {
    let count = 0;
    for (const [key, session] of sessions) {
      if (key !== activeKey && isBusy(session)) count++;
    }
    return count;
  }, [sessions, activeKey]);

  return {
    status: active.status,
    statusLabel: active.statusLabel,
    messages: active.messages,
    error: active.error,
    errorCode: active.errorCode,
    pendingApproval: active.pendingApproval,
    conversationId: active.conversationId,
    model,
    setModel,
    reasoningEffort,
    setReasoningEffort,
    fastMode,
    setFastMode,
    send,
    midTurn,
    editAndResend,
    retry,
    stop,
    confirmApproval,
    newSession,
    activateConversation,
    openConversation,
    discardConversation,
    busyConversationIds,
    backgroundBusyCount,
  };
}
