"use client";

import * as React from "react";
import { toast } from "react-toastify";
import {
  localAgentEffort,
  localAgentModel,
  localModelRefusalMessage,
  useLocalStatus,
} from "@/features/local-runtime";
import { composerSeed, rememberComposerPick } from "@/features/settings";
import { formatMsg, msg } from "@/shared/lib/messages";
import { I18N_KEY } from "@/shared/lib/i18n";
import { getActiveLocale } from "@/shared/lib/runtime-locale";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { webPicked } from "@/shared/lib/api";
import { parseSourceKey, sourceKey, type ApiSource } from "@/shared/lib/api-route";
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

import { confirmGeneralistApproval, streamCompaction, streamGeneralistAgent } from "../lib/stream";
import {
  chatWindow,
  compactionAnchor,
  parseCompaction,
  withCompaction,
  withServerCost,
  withSideCents,
} from "../lib/compaction";
import { loadConversation } from "../lib/conversation-api";
import {
  dropPendingToolCalls,
  pendingToolCall,
  placeToolCall,
  stopToolCalls,
} from "../lib/pending-tool-calls";
import { editMode, retryMode } from "../lib/edit-turn";
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
  /**
   * Open a conversation as the displayed session. A null id shows messages
   * the server has no copy of: the next turn saves a new conversation.
   */
  openConversation: (id: string | null, messages: AgentMessage[], options?: { moveQueue?: boolean }) => void;
  /** Drop a conversation's session (aborting its stream) after deletion. */
  discardConversation: (id: string) => void;
  /** Drop the displayed session (aborting its stream), even one with no server id yet. */
  discardActive: () => void;
  /** Conversation ids with a stream running or waiting in the queue. */
  busyConversationIds: ReadonlySet<string>;
  /** Busy sessions other than the displayed one (badge / pill signal). */
  backgroundBusyCount: number;
  /** The displayed chat's compaction in progress, or null. */
  compacting: ChatCompacting | null;
  /** Condense the displayed chat's older messages now (`/compact`). */
  compact: (instructions: string) => void;
  /** Read the displayed chat's saved costs again, for `/session`. */
  syncCost: () => void;
}

/** A compaction in progress: started by the context threshold, or by `/compact`. */
export interface ChatCompacting {
  reason: "threshold" | "manual";
  /** How many window turns the summary reads; null until the server says. */
  summarizedTurns?: number | null;
  /** When the compaction started (ms epoch), so its row keeps its time across a remount. */
  startedAt: number;
}

export interface UseGeneralistAgentArgs {
  wizardState: WizardState;
  trustMode: TrustMode;
  onToolStart?: (ev: ToolStartPayload, ctx: SessionEventContext) => void;
  onToolEnd?: (ev: ToolEndPayload, ctx: SessionEventContext) => void;
  onConversationMeta?: (id: string, title: string, ctx: SessionEventContext) => void;
  /** A model title for a new chat. It never settles the turn. */
  onConversationTitle?: (id: string, title: string, cutTitle: string) => void;
  /** A new chat's stream closed before its model title came; the server may still save one. */
  onConversationTitleMissed?: (id: string, cutTitle: string) => void;
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
  compacting: ChatCompacting | null;
}

/** A turn captured at send time, replayed verbatim when a slot frees up. */
interface PendingRun {
  message: string;
  chatHistory: ChatTurn[];
  /** The message index of each `chatHistory` turn, for the compaction's anchor. */
  historyIndices: number[];
  /** The summary of the messages before `chatHistory`, from the latest compaction. */
  compactionSummary: string | null;
  /** The index of this turn's user message. */
  userIndex: number;
  wizardState: WizardState;
  trustMode: TrustMode;
  regenerate: boolean;
  /** User turn this message edits; the backend drops its stored copy and every later row. */
  editUserTurn: { text: string; occurrence: number } | null;
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
  /** Edit target of the latest run until the server confirms it saved the turn. */
  unsavedEdit: { text: string; occurrence: number } | null;
  /** Where the latest turn runs; its approval confirm goes to the same place. */
  source: ApiSource;
  /** A hidden send that came during a manual /compact; it runs when the compaction ends. */
  afterCompact?: { message: string; override?: WizardState } | null;
  /** An edit cancelled a /compact the server may still save; read its cost when the turn ends. */
  syncCostAfterTurn?: "due" | "reading";
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
    compacting: null,
  };
}

function blankRuntime(): SessionRuntime {
  return {
    controller: null,
    replyBuf: "",
    persistentExtras: {},
    pendingRun: null,
    turnStart: 0,
    unsavedEdit: null,
    source: "local",
  };
}

// A manual /compact counts as busy: its chat must not be pruned mid-call,
// and the busy badges and counts must include it.
function isBusy(session: SessionView): boolean {
  return (
    session.status === "streaming" ||
    session.status === "queued" ||
    session.compacting?.reason === "manual"
  );
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

  // The panel is client-only (ssr:false), so the localStorage read is
  // hydration-safe.
  const [model, setModelState] = React.useState<string | null>(
    () => composerSeed(isLocalMode()).model,
  );
  // Mirrored in a ref so the streaming closures (retry, regenerate) always
  // send the current choice without re-memoizing the run machinery.
  const modelRef = React.useRef<string | null>(model);
  const setModel = React.useCallback((next: string | null) => {
    modelRef.current = next;
    setModelState(next);
  }, []);
  const [reasoningEffort, setReasoningEffortState] = React.useState<string | null>(() =>
    liveEffort(composerSeed(isLocalMode()).effort),
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
  // programmatic send sees the latest props.
  const snapshotRef = React.useRef({ wizardState, trustMode });
  React.useEffect(() => {
    snapshotRef.current = { wizardState, trustMode };
  }, [wizardState, trustMode]);

  // Controllers of streams past ``done`` that stay open for the chat title.
  const openTailsRef = React.useRef(new Set<AbortController>());
  // Streams that unmount aborts must not ask the gone panel to read titles.
  const unmountedRef = React.useRef(false);
  React.useEffect(() => {
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
    };
  }, []);

  const callbacksRef = React.useRef<{
    onToolStart: UseGeneralistAgentArgs["onToolStart"];
    onToolEnd: UseGeneralistAgentArgs["onToolEnd"];
    onConversationMeta: UseGeneralistAgentArgs["onConversationMeta"];
    onConversationTitle: UseGeneralistAgentArgs["onConversationTitle"];
    onConversationTitleMissed: UseGeneralistAgentArgs["onConversationTitleMissed"];
  }>({
    onToolStart: args.onToolStart,
    onToolEnd: args.onToolEnd,
    onConversationMeta: args.onConversationMeta,
    onConversationTitle: args.onConversationTitle,
    onConversationTitleMissed: args.onConversationTitleMissed,
  });
  React.useEffect(() => {
    callbacksRef.current = {
      onToolStart: args.onToolStart,
      onToolEnd: args.onToolEnd,
      onConversationMeta: args.onConversationMeta,
      onConversationTitle: args.onConversationTitle,
      onConversationTitleMissed: args.onConversationTitleMissed,
    };
  }, [
    args.onToolStart,
    args.onToolEnd,
    args.onConversationMeta,
    args.onConversationTitle,
    args.onConversationTitleMissed,
  ]);

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
      // After ``done`` the stream can stay open for the chat title, but it no
      // longer holds a slot. It still holds a connection, so the oldest such
      // tail closes when it would push the connections past the cap.
      while (openTailsRef.current.size + streamingKeysRef.current.size > MAX_PARALLEL_STREAMS) {
        const oldest = openTailsRef.current.values().next().value;
        if (!oldest) break;
        openTailsRef.current.delete(oldest);
        oldest.abort();
      }
      // Set at ``done``: a late error, or any event but the title and the
      // final stats, must not touch the turn that runs next.
      let settled = false;
      const ended = () => controller.signal.aborted || settled;

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

      // A chat stays where it started; a new chat starts on the side picked
      // in the header. The composer lists that side's models, so the turn
      // sends the pick resolved for that side.
      const savedId = sessionsRef.current.get(key)?.conversationId ?? null;
      const saved = savedId ? parseSourceKey(savedId) : null;
      const source: ApiSource = saved ? saved.source : webPicked() ? "hosted" : "local";
      rt.source = source;
      // A new chat's first turn waits for a model title until it comes.
      let titleWait: { id: string; cutTitle: string } | null = null;
      const steerKey = newSteerKey();
      const steerQueue = steerQueueFor(key);
      const applySteer = ({ ids, text, message_id }: { ids: string[]; text: string; message_id?: number }) => {
        if (!steerQueue.applied(ids)) return;
        if (!text.trim()) return;
        const message: AgentMessage = {
          role: "user", content: text, createdAt: Date.now(),
          ...(message_id !== undefined ? { id: message_id } : {}),
        };
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
      steerQueue.openTurn(agentSteerChannel(steerKey, applySteer, source));
      // The reply bubble closed by the latest applied steer, which the
      // finished turn's `assistant_message` may repeat.
      let replyBeforeSteer: string | null = null;

      // Every stream callback short-circuits on ``ended()`` (the final stats
      // only on the abort) so a slow stream replaced by a fresh ``send`` (or
      // by the unmount-time abort) cannot leak state writes or window events
      // into the live view.
      const hosted = source === "hosted";
      rememberComposerPick(isLocalMode(), modelRef.current, effortRef.current);
      void localAgentModel(modelRef.current, hosted).then((sentModel) =>
        streamGeneralistAgent(
          {
            user_message: run.message,
            chat_history: run.chatHistory,
            wizard_state: run.wizardState,
            trust_mode: run.trustMode,
            conversation_id: saved?.id ?? null,
            regenerate: run.regenerate,
            edit_user_text: run.editUserTurn?.text,
            edit_user_occurrence: run.editUserTurn?.occurrence,
            locale: getActiveLocale(),
            model: sentModel ?? undefined,
            reasoning_effort:
              localAgentEffort(modelRef.current, effortRef.current, sentModel, hosted) ??
              undefined,
            fast_mode: sentFastMode(fastRef.current, sentModel) || undefined,
            steer_key: steerKey,
            compaction_summary: run.compactionSummary,
          },
          {
            signal: controller.signal,
            onCompactionStart: ({ reason, summarizedTurns }) => {
              if (ended()) return;
              patchSession(key, {
                compacting: {
                  reason: reason === "manual" ? "manual" : "threshold",
                  summarizedTurns,
                  startedAt: Date.now(),
                },
              });
            },
            onCompaction: (raw) => {
              if (ended()) return;
              const entry = parseCompaction(raw);
              if (entry) {
                const anchor = compactionAnchor(run.historyIndices, entry.keptTurns, run.userIndex);
                patchMessages(key, (prev) => withCompaction(prev, anchor, entry));
              }
              patchSession(key, { compacting: null });
            },
            onCompactionError: ({ cents }) => {
              if (ended()) return;
              if (cents) patchMessages(key, (prev) => withSideCents(prev, run.userIndex, cents));
              patchSession(key, { compacting: null });
            },
            onSteerApplied: (ev) => {
              if (ended()) return;
              applySteer(ev);
            },
            onConversationMeta: (ev) => {
              if (ended()) return;
              if (!ev.conversation_id) return;
              rt.unsavedEdit = null;
              const id = sourceKey(source, ev.conversation_id);
              // The server starts a title only for a new chat with a cut title.
              if (savedId === null && ev.title && !titleWait) titleWait = { id, cutTitle: ev.title };
              patchSession(key, { conversationId: id });
              const messageId = ev.user_message_id;
              if (messageId !== undefined) {
                patchMessages(key, (prev) =>
                  prev[run.userIndex]?.role === "user"
                    ? prev.map((m, i) => (i === run.userIndex ? { ...m, id: messageId } : m))
                    : prev,
                );
              }
              callbacksRef.current.onConversationMeta?.(
                id,
                ev.title,
                eventContext(key),
              );
            },
            // The title can land after ``done``, while the next turn runs, so
            // it only renames the history row and never touches ``rt``.
            onConversationTitle: (ev) => {
              if (!ev.conversation_id) return;
              // An empty title means the server made none, so no reload can find one.
              titleWait = null;
              if (!ev.title) return;
              callbacksRef.current.onConversationTitle?.(
                sourceKey(source, ev.conversation_id),
                ev.title,
                ev.cut_title,
              );
            },
            onReasoningPatch: (chunk) => {
              if (ended()) return;
              patchThinking(key, (segments, toolCount, since) =>
                appendReasoning(segments, chunk, Date.now(), toolCount, since),
              );
            },
            onThinkingSummary: ({ index, text }) => {
              if (ended()) return;
              patchMessages(key, (prev) => summarizeThinking(prev, turnStart, index, text));
            },
            onPlan: (text) => {
              if (ended()) return;
              patchMessages(key, (prev) => {
                const last = prev[prev.length - 1];
                if (!last || last.role !== "assistant") return prev;
                const next = prev.slice();
                next[next.length - 1] = { ...last, plan: text };
                return next;
              });
            },
            onToolPending: ({ tool }) => {
              if (ended()) return;
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
              if (ended()) return;
              dropPendingRows();
            },
            onToolStart: (ev) => {
              if (ended()) return;
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
              if (ended()) return;
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
              if (ended()) return;
              patchSession(key, {
                pendingApproval: ev,
                statusLabel: msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.2"),
              });
            },
            onApprovalResolved: () => {
              if (ended()) return;
              patchSession(key, {
                pendingApproval: null,
                statusLabel: msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.3"),
              });
            },
            onMessagePatch: (chunk) => {
              if (ended()) return;
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
              if (ended()) return;
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
              if (ended()) return;
              settled = true;
              openTailsRef.current.add(controller);
              rt.unsavedEdit = null;
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
              if (ended()) return;
              // The server can still hold the stream open for the title.
              settled = true;
              openTailsRef.current.add(controller);
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
                compacting: null,
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
          source,
        ).finally(() => {
          openTailsRef.current.delete(controller);
          if (titleWait && !unmountedRef.current) {
            callbacksRef.current.onConversationTitleMissed?.(titleWait.id, titleWait.cutTitle);
          }
        }),
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
        rt.afterCompact = null;
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
      for (const tail of openTailsRef.current) tail.abort();
      openTailsRef.current.clear();
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
      editUserTurn: { text: string; occurrence: number } | null = null,
      compaction: AgentMessage["compaction"] = undefined,
    ) => {
      // A resend within the same session replaces its own in-flight or queued
      // turn — other sessions' streams are untouched.
      abortSession(key);
      const rt = getRuntime(key);
      rt.replyBuf = "";
      rt.unsavedEdit = editUserTurn;

      const userTurn: AgentMessage = {
        role: "user",
        content: userMessage,
        createdAt: Date.now(),
        ...(hidden ? { hidden } : {}),
        // A resend of the message a compaction is stored on keeps it, as the
        // server does: its summary covers only the turns before this one.
        ...(compaction ? { compaction } : {}),
      };
      // The window is read with the new message, so a compaction on it sends
      // its summary and none of the turns it summarized; then the message goes.
      const sent = chatWindow([...history, userTurn]);
      sent.turns.pop();
      sent.indices.pop();
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
        chatHistory: sent.turns,
        historyIndices: sent.indices,
        compactionSummary: sent.summary,
        userIndex: history.length,
        wizardState: ws,
        trustMode: tm,
        regenerate,
        editUserTurn,
      };

      commit((draft) => {
        const cur = draft.get(key);
        if (!cur) return;
        draft.set(key, {
          ...cur,
          messages: [...history, userTurn, { role: "assistant", content: "", toolCalls: [] }],
          error: null,
          errorCode: null,
          pendingApproval: null,
          compacting: null,
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
      // A message sent during a manual /compact waits in the chat's queue and
      // runs as the next turn when the compaction ends (Gilad, 2026-11-12).
      if (sessionsRef.current.get(key)?.compacting?.reason === "manual") {
        // A hidden send (a wizard Back, a screen kickoff) is not the user's
        // to edit, so it waits on the runtime instead of in the queue.
        if (options?.hidden) {
          getRuntime(key).afterCompact = { message: trimmed, override: wizardStateOverride };
          return;
        }
        const queue = steerQueueFor(key);
        queue.setBusy(true);
        queue.enqueue(trimmed);
        return;
      }
      const history = sessionsRef.current.get(key)?.messages ?? [];
      requestRun(key, trimmed, history, wizardStateOverride, false, options?.hidden ?? false);
    },
    [getRuntime, requestRun, steerQueueFor],
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
      // isBusy holds the queue during a manual /compact too; its end sends the next message.
      const busy = session ? isBusy(session) : false;
      queue.setBusy(busy, session?.status === "error");
    }
  }, [sessions, steerQueues, requestRun, activeKey]);

  const editAndResend = React.useCallback(
    (messageIndex: number, content: string) => {
      const trimmed = content.trim();
      if (!trimmed) return;
      const key = activeKeyRef.current;
      const current = sessionsRef.current.get(key)?.messages ?? [];
      const truncated = current.slice(0, messageIndex);
      const { regenerate, editUserTurn } = editMode(
        current,
        messageIndex,
        runtimesRef.current.get(key)?.unsavedEdit ?? null,
      );
      steerQueueFor(key).pause();
      const rt = runtimesRef.current.get(key);
      if (rt && sessionsRef.current.get(key)?.compacting?.reason === "manual") rt.syncCostAfterTurn = "due";
      // The edit replaces the history, not a hidden request (a wizard Back)
      // that waited for the /compact: it runs when the edited turn ends.
      const held = rt?.afterCompact ?? null;
      requestRun(
        key,
        trimmed,
        truncated,
        undefined,
        regenerate,
        false,
        editUserTurn,
        current[messageIndex]?.compaction,
      );
      if (rt && held) rt.afterCompact = held;
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
    const { regenerate, editUserTurn } = retryMode(
      runtimesRef.current.get(key)?.unsavedEdit ?? null,
    );
    steerQueueFor(key).pause();
    requestRun(
      key,
      lastUser.content,
      truncated,
      undefined,
      regenerate,
      lastUser.hidden ?? false,
      editUserTurn,
      lastUser.compaction,
    );
  }, [requestRun, steerQueueFor]);

  const stopSession = React.useCallback((key: string) => {
    steerQueueFor(key).pause();
    const wasQueued = sessionsRef.current.get(key)?.status === "queued";
    abortSession(key);
    patchSession(key, { status: "idle", statusLabel: "", compacting: null });
    patchThinking(key, (segments) => closeThinking(segments, Date.now()));
    patchMessages(key, (prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== "assistant" || !last.toolCalls?.some((t) => t.status === "running")) {
        return prev;
      }
      const next = prev.slice();
      next[next.length - 1] = { ...last, toolCalls: stopToolCalls(last.toolCalls, Date.now()) };
      return next;
    });
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

  // `/compact` streams on the chat's own controller. A send during it waits
  // in the chat's queue (see `send`). An edit still cancels it: the edit
  // rewrites the history that the summary is being written from.
  const compact = React.useCallback(
    (instructions: string) => {
      const key = activeKeyRef.current;
      const session = sessionsRef.current.get(key);
      if (!session || session.compacting) return;
      if (isBusy(session)) {
        toast.info(msg("agent.compact.wait_reply"));
        return;
      }
      const sent = chatWindow(session.messages);
      if (!sent.turns.some((t, i) => i > 0 && t.role === "user")) {
        toast.info(msg("agent.compact.nothing"));
        return;
      }
      const rt = getRuntime(key);
      rt.controller?.abort();
      const controller = new AbortController();
      rt.controller = controller;
      // Known limit: this stream is not counted against MAX_PARALLEL_STREAMS.
      // It cannot go into `openTailsRef`, because `startStream` aborts the
      // oldest entry there to free a connection, and that would cancel the
      // user's /compact. On HTTP/1.1, compacts in several chats plus 4 turns
      // can pass the browser's per-host connection cap and stall a request.
      const saved = session.conversationId ? parseSourceKey(session.conversationId) : null;
      const source: ApiSource = saved ? saved.source : webPicked() ? "hosted" : "local";
      const hosted = source === "hosted";
      const startedAt = Date.now();
      patchSession(key, { compacting: { reason: "manual", startedAt }, error: null, errorCode: null });
      const fail = (text: string) => toast.error(text);
      void localAgentModel(modelRef.current, hosted)
        .then((sentModel) =>
          streamCompaction(
            {
              chat_history: sent.turns,
              compaction_summary: sent.summary,
              instructions: instructions.trim() || undefined,
              conversation_id: saved?.id ?? null,
              model: sentModel ?? undefined,
              reasoning_effort:
                localAgentEffort(modelRef.current, effortRef.current, sentModel, hosted) ?? undefined,
              fast_mode: sentFastMode(fastRef.current, sentModel) || undefined,
            },
            {
              signal: controller.signal,
              onCompactionStart: ({ summarizedTurns }) => {
                if (controller.signal.aborted) return;
                patchSession(key, { compacting: { reason: "manual", summarizedTurns, startedAt } });
              },
              onCompaction: (raw) => {
                if (controller.signal.aborted) return;
                const entry = parseCompaction(raw);
                if (!entry) return;
                const anchor = compactionAnchor(sent.indices, entry.keptTurns, null);
                patchMessages(key, (prev) => withCompaction(prev, anchor, entry));
              },
              // The backend adds a failed call's cost to the latest saved message too.
              onCompactionError: ({ cents }) => {
                if (cents) patchMessages(key, (prev) => withSideCents(prev, null, cents));
                if (!controller.signal.aborted) fail(msg("agent.compact.failed"));
              },
              onDone: () => {},
              onError: (message, code) => {
                if (controller.signal.aborted) return;
                fail(
                  code === "nothing_to_compact"
                    ? msg("agent.compact.nothing")
                    : code === I18N_KEY.BILLING_INSUFFICIENT_FUNDS
                      ? msg("agent.error.insufficient_funds")
                      : message,
                );
              },
            },
            source,
          ),
        )
        .catch(() => {
          if (!controller.signal.aborted) fail(msg("agent.compact.failed"));
        })
        .finally(() => {
          // An aborted compact returns first: the held send may belong to a
          // newer compact, and abortSession already dropped this one's.
          if (controller.signal.aborted) return;
          const held = rt.afterCompact;
          rt.afterCompact = null;
          if (rt.controller === controller) rt.controller = null;
          patchSession(key, { compacting: null });
          const now = sessionsRef.current.get(key);
          // The held hidden send runs before the user's queued messages, on
          // purpose: it starts in the same commit that ends the compaction, so
          // the queue sees a busy chat. A wizard Back or a screen kickoff must
          // reach the agent before the replies that the user typed after it.
          if (held && now) requestRun(key, held.message, now.messages, held.override, false, true);
        });
    },
    [getRuntime, patchMessages, patchSession, requestRun],
  );

  // A stop during a summary call leaves its cost unknown here; the server
  // saves it when the call ends, so `/session` reads the saved copy.
  /** Read the saved cost of a chat; resolves true when the chat took it. */
  const syncCostOf = React.useCallback(
    async (key: string): Promise<boolean> => {
      const id = sessionsRef.current.get(key)?.conversationId;
      if (!id) return false;
      const { detail } = await loadConversation(id);
      const now = sessionsRef.current.get(key);
      if (!detail || !now || now.conversationId !== id || isBusy(now)) return false;
      patchMessages(key, (prev) => withServerCost(prev, detail.messages));
      return true;
    },
    [patchMessages],
  );
  const syncCost = React.useCallback(() => void syncCostOf(activeKeyRef.current), [syncCostOf]);

  // The server saves a summary call that an edit cut off when the call ends;
  // the edit's turn runs first, so its end reads the saved cost.
  React.useEffect(() => {
    for (const [key, session] of sessions) {
      const rt = runtimesRef.current.get(key);
      if (rt?.syncCostAfterTurn !== "due" || isBusy(session)) continue;
      // A failed read stays due, so the chat reads again when it is next idle.
      rt.syncCostAfterTurn = "reading";
      void syncCostOf(key)
        .catch(() => false)
        .then((ok) => {
          if (rt.syncCostAfterTurn === "reading") rt.syncCostAfterTurn = ok ? undefined : "due";
        });
    }
  }, [sessions, syncCostOf]);

  // A hidden send that an edit carried past a cancelled /compact runs when
  // the edited turn ends. A stop or a new run drops it (`abortSession`).
  React.useEffect(() => {
    for (const [key, session] of sessions) {
      const rt = runtimesRef.current.get(key);
      const held = rt?.afterCompact;
      if (!rt || !held || isBusy(session) || session.status === "error") continue;
      rt.afterCompact = null;
      requestRun(key, held.message, session.messages, held.override, false, true);
    }
  }, [sessions, requestRun]);

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
    (id: string | null, loaded: AgentMessage[], options?: { moveQueue?: boolean }) => {
      const from = options?.moveQueue ? steerQueues.get(activeKeyRef.current) : undefined;
      const moved = (from?.getSnapshot().queued ?? []).flatMap((item) => from?.take(item.id) ?? []);
      const key = nextKey();
      commit((draft) => {
        draft.set(key, {
          ...blankSession(key),
          conversationId: id,
          messages: loaded,
        });
        pruneInactive(draft, key);
      });
      setActive(key);
      if (moved.length === 0) return;
      // Held, as on the old chat, so no follow-up runs ahead of a resent turn.
      const queue = steerQueueFor(key);
      for (const text of moved) queue.enqueue(text);
      queue.pause();
    },
    [commit, nextKey, pruneInactive, setActive, steerQueues, steerQueueFor],
  );

  const discardSession = React.useCallback(
    (key: string) => {
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

  const discardConversation = React.useCallback(
    (id: string) => {
      for (const [key, session] of sessionsRef.current) {
        if (session.conversationId === id) {
          discardSession(key);
          return;
        }
      }
    },
    [discardSession],
  );

  const discardActive = React.useCallback(() => discardSession(activeKeyRef.current), [discardSession]);

  const active = sessions.get(activeKey) ?? blankSession(activeKey);

  const activeSteerQueue = steerQueueFor(activeKey);
  const steerSnapshot = React.useSyncExternalStore(
    activeSteerQueue.subscribe,
    activeSteerQueue.getSnapshot,
    activeSteerQueue.getSnapshot,
  );
  const midTurn = React.useMemo(
    () => midTurnOf(() => activeSteerQueue, steerSnapshot),
    [activeSteerQueue, steerSnapshot],
  );

  const confirmApproval = React.useCallback(
    async (approved: boolean) => {
      const key = activeKeyRef.current;
      const pa = sessionsRef.current.get(key)?.pendingApproval;
      if (!pa) return;
      patchSession(key, { pendingApproval: null });
      const source = runtimesRef.current.get(key)?.source ?? "local";
      const resolved = await confirmGeneralistApproval(pa.id, approved, source);
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
    discardActive,
    busyConversationIds,
    backgroundBusyCount,
    compacting: active.compacting,
    compact,
    syncCost,
  };
}
