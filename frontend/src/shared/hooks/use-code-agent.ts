"use client";

import * as React from "react";
import { useCompletionNotification } from "@/shared/hooks/use-completion-notification";
import { formatMsg, msg } from "@/shared/lib/messages";

import {
  streamCodeAgent,
  type AgentQuestion,
  type CodeAgentToolName,
  type CodeAgentVerification,
} from "@/shared/lib/api";
import type { MessageKey } from "@/shared/lib/generated/ui-catalog";
import { getActiveLocale } from "@/shared/lib/runtime-locale";
import { liveLanesLast } from "@/shared/hooks/reasoning-order";
import { appendVersion, type ArtifactVersion } from "@/shared/hooks/artifact-versions";
import { createCodeDrafts, type DraftSource } from "@/shared/hooks/code-drafts";
import { TERMS } from "@/shared/lib/terms";
import type { ParsedDataset } from "@/shared/lib/parse-dataset";
import type { ValidateCodeResponse, WorkflowSpec } from "@/shared/types/api";
import type { UnifiedAuthoringContext } from "@/shared/types/unified-run";
import type { TurnStats } from "@/shared/ui/agent/types";
import type { MidTurnQueue } from "@/shared/ui/agent/steer-queue";
import { agentSteerChannel, newSteerKey, useSteerQueue } from "@/shared/ui/agent/use-steer-queue";

type AgentStatus = "idle" | "streaming" | "done" | "error";
type AgentMode = "seed" | "chat";
export type ArtifactStatus = "idle" | "waiting" | "writing" | "done";

type AgentToolName = CodeAgentToolName;
type AgentToolStatus = "running" | "done" | "error";

// Unified runs drive the same two editor slots under their own tool names:
// the signature slot holds run code, the metric slot the metric.
function isSeedTool(tool: AgentToolName): boolean {
  return tool === "edit_signature" || tool === "edit_run" || tool === "edit_seed";
}

function isScorerTool(tool: AgentToolName): boolean {
  return tool === "edit_metric" || tool === "edit_scorer";
}

export interface AgentToolCall {
  id: string;
  tool: AgentToolName;
  reason: string;
  status: AgentToolStatus;
  startedAt: number;
  endedAt: number | null;
  prevCode?: string;
  newCode?: string;
  /** The workspace file a write_file / delete_file / open_file call is about. */
  path?: string;
  /** The 1-based line an open_file call showed. */
  line?: number;
  /** A verify_code call's per-file result, once it arrived. */
  verification?: CodeAgentVerification;
}

// Per-index line diff — returns 1-based line numbers in `next` that differ
// from `prev`. Good enough for flashing small refinements; if the agent
// restructures heavily, most lines will flash (acceptable).
function diffChangedLines(prev: string, next: string): number[] {
  if (!prev.trim()) return [];
  const prevLines = prev.split("\n");
  const nextLines = next.split("\n");
  const changed: number[] = [];
  for (let i = 0; i < nextLines.length; i++) {
    if (nextLines[i] !== prevLines[i]) changed.push(i + 1);
  }
  return changed;
}

// Compact the backend ValidateCodeResponse into a short string the agent can
// read on the next turn — gives it concrete "what's broken" context so the
// retry is targeted, not another blind rewrite.
function summarizeValidation(v: ValidateCodeResponse | null): string {
  if (!v) return "";
  if (v.valid && v.warnings.length === 0) return "OK";
  const parts: string[] = [];
  if (v.errors.length > 0) parts.push(`errors: ${v.errors.join(" | ")}`);
  if (v.warnings.length > 0) parts.push(`warnings: ${v.warnings.join(" | ")}`);
  return parts.join("; ");
}

const MAX_AUTO_FIX = 2;

// Synthetic opener shown as a user bubble before the seed reply. It gives
// the conversation a natural starting point — the AI's first message reads
// as a response to a real request, not a monologue — and lets the user
// revise it via the edit pencil to steer the initial generation. Resolved on
// call (not at module scope) so it can't capture a raw key before the message
// shim has loaded.
function seedUserMessage(unified: boolean): string {
  if (unified) return unifiedMsg("submit.v2.metric.agent.seed_request");
  return formatMsg("auto.features.submit.hooks.use.code.agent.template.1", {
    p1: TERMS.dataset,
  });
}

// The unified wizard's agent copy is new; its keys land in the generated
// catalog with the wizard v2 strings.
function unifiedMsg(key: string): string {
  return msg(key as MessageKey);
}

// Seed mode runs two authors (signature+metric, or graph+metric) in parallel
// over one multiplexed SSE stream. Keyed buffers keep each stream's reasoning
// contiguous; multi-stream runs render as labeled sections instead of
// token-interleaved gibberish.
interface ReasoningSections {
  order: string[];
  bufs: Record<string, string>;
}

function reasoningSourceLabel(source: string): string {
  if (source === "signature") return TERMS.signature;
  if (source === "metric") return TERMS.metric;
  // Module names are technical terms kept in English throughout the UI.
  if (source === "workflow") return "Workflow";
  return source;
}

function composeReasoning(sections: ReasoningSections, live: ReadonlySet<string>): string {
  const first = sections.order[0];
  if (first === undefined) return "";
  if (sections.order.length === 1) return sections.bufs[first] ?? "";
  return liveLanesLast(sections.order, live)
    .map((key) => `[${reasoningSourceLabel(key)}]\n${sections.bufs[key] ?? ""}`)
    .join("\n\n");
}

export interface AgentMessage {
  role: "assistant" | "user";
  content: string;
  toolCalls?: AgentToolCall[];
  model?: string | null;
  /** Concrete model selected by Auto Router, when the route was automatic. */
  servedModel?: string | null;
  stats?: TurnStats | null;
  /** A turn the app sent on the user's behalf; the transcript leaves its bubble out. */
  hidden?: boolean;
  /** When the message was sent, in epoch ms. */
  createdAt?: number;
}

export interface CodeAgentState {
  status: AgentStatus;
  mode: AgentMode;
  statusLabel: string;
  signatureStatus: ArtifactStatus;
  metricStatus: ArtifactStatus;
  /**
   * The run code's / the metric's first chunk of this writing stretch has
   * reached its editor. A lane is "writing" from the run's start, seconds
   * before any code arrives, so "Editing run.py" waits on this.
   */
  signatureFlowing: boolean;
  metricFlowing: boolean;
  messages: AgentMessage[];
  error: string | null;
  /** The wizard agent's daily turn cap is used up. */
  limitReached: boolean;
  canSend: boolean;
  /** The question the agent's last reply asked, with clickable answers. */
  question: AgentQuestion | null;
  signatureVersions: ArtifactVersion[];
  metricVersions: ArtifactVersion[];
  /** Each extra workspace file's versions the agent wrote, by path. */
  fileVersions: Record<string, ArtifactVersion[]>;
  /** Extra workspace files the agent is writing right now. */
  writingFiles: string[];
  /** The last verify_code result, or null before any check. */
  verification: CodeAgentVerification | null;
  signatureVersionIndex: number;
  metricVersionIndex: number;
  signatureFlashLines: number[];
  metricFlashLines: number[];
  reasoning: string;
  reasoningStartedAt: number | null;
  reasoningEndedAt: number | null;
  goToSignatureVersion: (index: number) => void;
  goToMetricVersion: (index: number) => void;
  /** Send a chat turn; ``hidden`` sends it without a user bubble (a kickoff the app started). */
  send: (message: string, options?: { hidden?: boolean }) => void;
  /** Messages sent while a turn runs: steered into it or queued after it. */
  midTurn: MidTurnQueue;
  editAndResend: (messageIndex: number, content: string) => void;
  retry: () => void;
  stop: () => void;
  reset: () => void;
}

export interface UseCodeAgentArgs {
  // Plain string roles (input/output/ignore); this hook only reads the
  // "input"/"output" roles.
  columnRoles: Record<string, string>;
  columnKinds: Record<string, "text" | "image">;
  parsedDataset: ParsedDataset | null;
  moduleName: string;
  signatureCode: string;
  metricCode: string;
  setSignatureCode: (v: string) => void;
  setMetricCode: (v: string) => void;
  signatureManuallyEdited: boolean;
  metricManuallyEdited: boolean;
  setSignatureManuallyEdited: (v: boolean) => void;
  setMetricManuallyEdited: (v: boolean) => void;
  setSignatureValidation: (v: ValidateCodeResponse | null) => void;
  setMetricValidation: (v: ValidateCodeResponse | null) => void;
  signatureValidation: ValidateCodeResponse | null;
  metricValidation: ValidateCodeResponse | null;
  runSignatureValidation: (overrideCode?: string) => Promise<unknown>;
  runMetricValidation: (overrideCode?: string) => Promise<unknown>;
  // Workflow mode (module_name === "workflow"): the agent authors the graph
  // instead of a single signature. `workflowTouched` guards the auto-seed
  // like the manual-edit flags do for code; `applyAgentWorkflow` lands a
  // graph snapshot on the canvas (with the changed node pulsed).
  isWorkflow?: boolean;
  workflowSpec?: WorkflowSpec | null;
  workflowTouched?: boolean;
  applyAgentWorkflow?: (spec: WorkflowSpec, changedNodeId: string | null) => void;
  // Gates the auto-seed while the wizard's module picker is still open; the
  // seed fires as soon as this flips true (i.e. the user picked a module).
  seedEnabled?: boolean;
  // Directives confirmed at the end of the Signature & Metric interview;
  // the seed authors honor them. Empty when the interview was skipped.
  interviewBrief?: string[];
  /** Unified (wizard v2) context; when set the agent drafts run code in the
   *  signature slot and the metric in the metric slot, and no dataset is
   *  required. */
  unified?: UnifiedAuthoringContext | null;
  /** Lands objective / background text the agent wrote. */
  onBrief?: (fields: { objective?: string; background?: string }) => void;
  /** Unified: the workspace's files besides run.py and metric.py. */
  files?: Record<string, string>;
  /** Unified: write one extra file's text, or delete it with null. */
  setFile?: (path: string, text: string | null) => void;
  /** Unified: show a workspace file in the editor, at a 1-based line when given (open_file). */
  onOpenFile?: (path: string, line?: number) => void;
  // Catalog model id + effort the code author runs on (the composer's model
  // menu). Absent/`null` routes automatically. The agent panel forwards the
  // conversation's chosen model so code authoring follows the composer.
  model?: string | null;
  /** Maps `model` to the id a request sends, at send time (local CLI roles). */
  resolveModel?: (model: string | null) => Promise<string | null>;
  reasoningEffort?: string | null;
  /** Maps the effort to the one a request sends with the resolved model (local CLI roles). */
  resolveEffort?: (model: string | null, effort: string | null, sent: string | null) => string | null;
  /** The chat a reload brings back; read once, on mount. */
  initialMessages?: AgentMessage[];
}

const DAILY_LIMIT_CODE = "wizard_agent.daily_limit_reached";

export function useCodeAgent(args: UseCodeAgentArgs): CodeAgentState {
  const {
    columnRoles,
    columnKinds,
    parsedDataset,
    moduleName,
    signatureCode,
    metricCode,
    setSignatureCode,
    setMetricCode,
    signatureManuallyEdited,
    metricManuallyEdited,
    setSignatureManuallyEdited,
    setMetricManuallyEdited,
    setSignatureValidation,
    setMetricValidation,
    signatureValidation,
    metricValidation,
    runSignatureValidation,
    runMetricValidation,
    isWorkflow = false,
    workflowSpec = null,
    workflowTouched = false,
    applyAgentWorkflow,
    seedEnabled = true,
    interviewBrief,
    model,
    resolveModel,
    reasoningEffort,
    resolveEffort,
    unified = null,
    onBrief,
    files,
    setFile,
    onOpenFile,
    initialMessages,
  } = args;

  const [status, setStatus] = React.useState<AgentStatus>("idle");
  // The run notifies from the hook, so it still does after its panel has left the screen.
  useCompletionNotification(status === "streaming", () =>
    msg(status === "error" ? "notify.code.failed" : "notify.code.done"),
  );
  const [mode, setMode] = React.useState<AgentMode>("seed");
  const [statusLabel, setStatusLabel] = React.useState("");
  const [signatureStatus, setSignatureStatus] = React.useState<ArtifactStatus>("idle");
  const [metricStatus, setMetricStatus] = React.useState<ArtifactStatus>("idle");
  const [flowing, setFlowing] = React.useState({ signature: false, metric: false });
  const markFlowing = React.useCallback((source: "signature" | "metric", on: boolean) => {
    setFlowing((prev) => (prev[source] === on ? prev : { ...prev, [source]: on }));
  }, []);
  const [messages, setMessages] = React.useState<AgentMessage[]>(() => initialMessages ?? []);
  const [error, setError] = React.useState<string | null>(null);
  const [limitReached, setLimitReached] = React.useState(false);
  const [question, setQuestion] = React.useState<AgentQuestion | null>(null);
  const [signatureVersions, setSignatureVersions] = React.useState<ArtifactVersion[]>([]);
  const [metricVersions, setMetricVersions] = React.useState<ArtifactVersion[]>([]);
  const [fileVersions, setFileVersions] = React.useState<Record<string, ArtifactVersion[]>>({});
  const [writingFiles, setWritingFiles] = React.useState<string[]>([]);
  const [verification, setVerification] = React.useState<CodeAgentVerification | null>(null);
  const [signatureVersionIndex, setSignatureVersionIndex] = React.useState(-1);
  const [metricVersionIndex, setMetricVersionIndex] = React.useState(-1);
  const [signatureFlashLines, setSignatureFlashLines] = React.useState<number[]>([]);
  const [metricFlashLines, setMetricFlashLines] = React.useState<number[]>([]);
  const [reasoning, setReasoning] = React.useState("");
  const [reasoningStartedAt, setReasoningStartedAt] = React.useState<number | null>(null);
  const [reasoningEndedAt, setReasoningEndedAt] = React.useState<number | null>(null);

  const abortRef = React.useRef<AbortController | null>(null);
  const sigBufRef = React.useRef("");
  const metricBufRef = React.useRef("");
  const replyBufRef = React.useRef("");
  const reasoningBufRef = React.useRef("");
  const reasoningSectionsRef = React.useRef<ReasoningSections>({ order: [], bufs: {} });
  // The timer stops once no stream is left reasoning: the seed's run and
  // metric authors reason in parallel, and one can still be thinking for
  // minutes after the other's code has landed.
  const reasoningEndedRef = React.useRef(false);
  // Streams that have reasoned but not yet started writing their output.
  const reasoningLanesRef = React.useRef<Set<string>>(new Set());
  const autoRanRef = React.useRef(false);
  const [sessionKey, setSessionKey] = React.useState(0);
  const pendingValidationsRef = React.useRef<
    Array<{ kind: "signature" | "metric"; promise: Promise<unknown> }>
  >([]);
  const autoFixAttemptsRef = React.useRef(0);
  const runAgentRef = React.useRef<
    ((msg: string, hist: AgentMessage[]) => void) | null
  >(null);
  const messagesRef = React.useRef<AgentMessage[]>([]);
  // `send` is declared after `runAgent`, which needs the queue's store.
  const sendLatestRef = React.useRef<(text: string) => void>(() => {});
  const sendQueued = React.useCallback((text: string) => sendLatestRef.current(text), []);
  const { store: steerStore, midTurn } = useSteerQueue({
    busy: status === "streaming",
    send: sendQueued,
  });

  const flashClearRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  // Latest values for the stream callbacks — kept in a ref so closures
  // created inside `runAgent` stay in sync without re-creating the callback.
  const snapshotRef = React.useRef({
    signatureCode,
    metricCode,
    signatureValidation,
    metricValidation,
    workflowSpec,
    files,
  });
  React.useEffect(() => {
    snapshotRef.current = {
      signatureCode,
      metricCode,
      signatureValidation,
      metricValidation,
      workflowSpec,
      files,
    };
  }, [signatureCode, metricCode, signatureValidation, metricValidation, workflowSpec, files]);
  const setFileRef = React.useRef(setFile);
  React.useEffect(() => {
    setFileRef.current = setFile;
  }, [setFile]);

  // Revert support across the conversation (first graph ever seen).
  const initialWorkflowRef = React.useRef<WorkflowSpec | null>(null);
  const applyAgentWorkflowRef = React.useRef(applyAgentWorkflow);
  React.useEffect(() => {
    applyAgentWorkflowRef.current = applyAgentWorkflow;
  }, [applyAgentWorkflow]);

  const runnersRef = React.useRef({ runSignatureValidation, runMetricValidation });
  React.useEffect(() => {
    runnersRef.current = { runSignatureValidation, runMetricValidation };
  }, [runSignatureValidation, runMetricValidation]);

  const versionsRef = React.useRef({ signatureVersions, metricVersions });
  React.useEffect(() => {
    versionsRef.current = { signatureVersions, metricVersions };
  }, [signatureVersions, metricVersions]);

  React.useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  const onBriefRef = React.useRef(onBrief);
  React.useEffect(() => {
    onBriefRef.current = onBrief;
  }, [onBrief]);
  const onOpenFileRef = React.useRef(onOpenFile);
  React.useEffect(() => {
    onOpenFileRef.current = onOpenFile;
  }, [onOpenFile]);

  const hasRequiredContext = React.useMemo(() => {
    if (unified) return unified.task.objective.trim().length > 0;
    if (!parsedDataset || parsedDataset.rowCount === 0) return false;
    const hasInput = Object.values(columnRoles).some((r) => r === "input");
    const hasOutput = Object.values(columnRoles).some((r) => r === "output");
    return hasInput && hasOutput;
  }, [parsedDataset, columnRoles, unified]);
  // A unified chat needs nothing up front: with no objective yet, the agent
  // asks for it. Only the seed draft waits for one.
  const canChat = unified ? true : hasRequiredContext;

  const appendReply = React.useCallback((chunk: string) => {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== "assistant") return prev;
      const next = prev.slice();
      next[next.length - 1] = { ...last, content: last.content + chunk };
      return next;
    });
  }, []);

  const pushToolCall = React.useCallback((call: AgentToolCall) => {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== "assistant") return prev;
      const calls = last.toolCalls ?? [];
      const next = prev.slice();
      // A streamed edit opens its row before the call runs; the call's own
      // start then fills in the reason on that same row.
      next[next.length - 1] = {
        ...last,
        toolCalls: calls.some((t) => t.id === call.id)
          ? calls.map((t) => (t.id === call.id ? { ...t, reason: call.reason || t.reason } : t))
          : [...calls, call],
      };
      return next;
    });
  }, []);

  const finishToolCall = React.useCallback((id: string, status: AgentToolStatus) => {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== "assistant" || !last.toolCalls?.length) return prev;
      const next = prev.slice();
      next[next.length - 1] = {
        ...last,
        toolCalls: last.toolCalls.map((t) =>
          t.id === id ? { ...t, status, endedAt: Date.now() } : t,
        ),
      };
      return next;
    });
  }, []);

  const attachCodeToLatestRunningToolCall = React.useCallback(
    (slot: "seed" | "scorer", prevCode: string, newCode: string) => {
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (!last || last.role !== "assistant" || !last.toolCalls?.length) return prev;
        const calls = last.toolCalls;
        let targetIdx = -1;
        for (let i = calls.length - 1; i >= 0; i--) {
          const c = calls[i];
          if (
            c &&
            c.status === "running" &&
            (slot === "seed" ? isSeedTool(c.tool) : isScorerTool(c.tool))
          ) {
            targetIdx = i;
            break;
          }
        }
        if (targetIdx < 0) return prev;
        const updatedCalls = calls.slice();
        const target = updatedCalls[targetIdx];
        if (!target) return prev;
        updatedCalls[targetIdx] = { ...target, prevCode, newCode };
        const next = prev.slice();
        next[next.length - 1] = { ...last, toolCalls: updatedCalls };
        return next;
      });
    },
    [],
  );

  const pushSignatureVersion = React.useCallback((code: string) => {
    // Stamp now, not in the updater: React runs it at render, after a check
    // that started in the same batch, which then sorted above this edit.
    const ts = Date.now();
    setSignatureVersions((prev) => {
      const next = appendVersion(prev, code, ts);
      if (next !== prev) setSignatureVersionIndex(next.length - 1);
      return next;
    });
  }, []);
  const pushFileVersion = React.useCallback((path: string, code: string, ts = Date.now()) => {
    setFileVersions((prev) => {
      const versions = prev[path] ?? [];
      const next = appendVersion(versions, code, ts);
      return next === versions ? prev : { ...prev, [path]: next };
    });
  }, []);
  const updateToolCall = React.useCallback((id: string, patch: Partial<AgentToolCall>) => {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== "assistant" || !last.toolCalls?.some((t) => t.id === id)) return prev;
      const next = prev.slice();
      next[next.length - 1] = {
        ...last,
        toolCalls: last.toolCalls.map((t) => (t.id === id ? { ...t, ...patch } : t)),
      };
      return next;
    });
  }, []);
  const pushMetricVersion = React.useCallback((code: string) => {
    // Stamp now, not in the updater: React runs it at render, after a check
    // that started in the same batch, which then sorted above this edit.
    const ts = Date.now();
    setMetricVersions((prev) => {
      const next = appendVersion(prev, code, ts);
      if (next !== prev) setMetricVersionIndex(next.length - 1);
      return next;
    });
  }, []);

  const runAgent = React.useCallback(
    (userMessage: string, history: AgentMessage[], hidden = false) => {
      const isChat = userMessage.length > 0;
      if (!(isChat ? canChat : hasRequiredContext)) return;

      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      sigBufRef.current = "";
      metricBufRef.current = "";
      setFlowing({ signature: false, metric: false });
      replyBufRef.current = "";
      reasoningBufRef.current = "";
      reasoningSectionsRef.current = { order: [], bufs: {} };
      reasoningEndedRef.current = false;
      // The unified seed's metric lane can reason for minutes before its
      // first token arrives, so both lanes count as thinking from the start.
      reasoningLanesRef.current = new Set(!isChat && unified ? ["signature", "metric"] : []);
      pendingValidationsRef.current = [];
      setQuestion(null);

      setMode(isChat ? "chat" : "seed");
      setStatus("streaming");
      setLimitReached(false);
      setStatusLabel(
        isChat
          ? msg("auto.features.submit.hooks.use.code.agent.literal.1")
          : unified
            ? unifiedMsg("submit.v2.metric.agent.reading")
            : formatMsg("auto.features.submit.hooks.use.code.agent.template.2", {
                p1: TERMS.dataset,
              }),
      );
      // The unified seed starts both authors at once, so each is being
      // worked on from the first moment, whether or not its model streams
      // its reasoning.
      const seedStatus: ArtifactStatus = isChat ? "idle" : unified ? "writing" : "waiting";
      setSignatureStatus(seedStatus);
      setMetricStatus(seedStatus);
      setReasoning("");
      setReasoningStartedAt(Date.now());
      setReasoningEndedAt(null);
      setError(null);

      if (isChat) {
        setMessages((m) => [
          ...m,
          { role: "user", content: userMessage, createdAt: Date.now(), ...(hidden ? { hidden } : {}) },
          { role: "assistant", content: "", toolCalls: [] },
        ]);
      } else {
        setMessages((m) => [
          ...m,
          { role: "user", content: seedUserMessage(!!unified) },
          { role: "assistant", content: "", toolCalls: [] },
        ]);
      }

      const sampleRows = (parsedDataset?.rows.slice(0, 5) ?? []) as Array<Record<string, unknown>>;
      const snapshot = snapshotRef.current;
      const { signatureVersions: sigVers, metricVersions: metVers } = versionsRef.current;
      const initialSignature = sigVers[0]?.code ?? snapshot.signatureCode;
      const initialMetric = metVers[0]?.code ?? snapshot.metricCode;
      // Track the prevCode for replace callbacks within this run. The outer
      // `snapshot` is captured at run-start, so reading it on the second
      // replace would diff against the run's starting code instead of the
      // previous version — the tool-call card would show a stale diff.
      let lastSignatureCode = snapshot.signatureCode;
      let lastMetricCode = snapshot.metricCode;
      // What each editor holds right now, streamed or replaced, so a lane
      // that finishes can land its code as a version straight away.
      let latestSignature = snapshot.signatureCode;
      let latestMetric = snapshot.metricCode;
      // The code an edit call is writing right now, shown live in its editor
      // and never saved as a version: only the call's checked replace is.
      const drafts = createCodeDrafts();
      const dropDraft = (source: DraftSource) => {
        if (!drafts.drop(source)) return;
        if (source === "signature") setSignatureCode(lastSignatureCode);
        else setMetricCode(lastMetricCode);
      };
      // Extra files: what each held before the agent's draft, so a write
      // that never lands puts it back (null: the file did not exist).
      const latestFiles: Record<string, string> = { ...(snapshot.files ?? {}) };
      const fileBefore = new Map<string, string | null>();
      const fileDraft = new Map<string, string>();
      const stopWriting = (path: string) =>
        setWritingFiles((prev) => (prev.includes(path) ? prev.filter((p) => p !== path) : prev));
      const dropFileDraft = (path: string) => {
        fileDraft.delete(path);
        if (!fileBefore.has(path)) return;
        setFileRef.current?.(path, fileBefore.get(path) ?? null);
        fileBefore.delete(path);
      };
      const dropFileDrafts = () => {
        for (const path of [...fileBefore.keys()]) dropFileDraft(path);
        setWritingFiles([]);
      };
      controller.signal.addEventListener("abort", () => {
        dropDraft("signature");
        dropDraft("metric");
        dropFileDrafts();
      });
      const chatHistory = history
        .filter((m) => m.content.trim().length > 0)
        .map((m) => ({ role: m.role, content: m.content }));

      // Send only image-typed entries — the backend defaults the rest to
      // text. Keeps the payload lean and makes the wire shape symmetric
      // with the wizard's mental model: text is the default.
      const imageColumnKinds: Record<string, "image"> = {};
      for (const [col, kind] of Object.entries(columnKinds)) {
        if (kind === "image") imageColumnKinds[col] = "image";
      }

      const priorWorkflow = isWorkflow ? (snapshot.workflowSpec ?? null) : null;
      if (isWorkflow && !initialWorkflowRef.current && priorWorkflow) {
        initialWorkflowRef.current = priorWorkflow;
      }

      // Stop the thinking timer as soon as artifact output starts flowing —
      // otherwise "Thinking" keeps pulsing through the whole code-writing
      // stretch (which renders in the editors, not the chat) and the stream
      // reads as stuck. `source` names the stream that moved on to output;
      // without one, every stream has.
      const markReasoningDone = (source?: string) => {
        if (source === undefined) reasoningLanesRef.current.clear();
        else reasoningLanesRef.current.delete(source);
        const recomposed = composeReasoning(reasoningSectionsRef.current, reasoningLanesRef.current);
        if (recomposed !== reasoningBufRef.current) {
          reasoningBufRef.current = recomposed;
          setReasoning(recomposed);
        }
        if (reasoningLanesRef.current.size > 0) return;
        if (reasoningBufRef.current && !reasoningEndedRef.current) {
          reasoningEndedRef.current = true;
          setReasoningEndedAt(Date.now());
        } else if (!reasoningBufRef.current) {
          // A model that streams no reasoning would otherwise keep a bare
          // "Thinking" row up while its code and reply are already flowing.
          setReasoningStartedAt(null);
        }
      };

      // Chat turns take messages the user sends while they run.
      const steerKey = isChat ? newSteerKey() : null;
      if (steerKey) steerStore.openTurn(agentSteerChannel(steerKey));
      // Reply text streamed before the latest applied steer, which the
      // finished turn's whole-reply `assistant_message` would repeat.
      let replyBeforeSteer: string | null = null;
      let metricLaneDone = false;
      let runLaneDone = false;

      // The effort goes with the model the request resolved.
      const effortField = (sent: string | null | undefined) => {
        const effort = resolveEffort
          ? resolveEffort(model ?? null, reasoningEffort ?? null, sent ?? null)
          : reasoningEffort;
        return effort ? { reasoning_effort: effort } : {};
      };
      void (resolveModel ? resolveModel(model ?? null) : Promise.resolve(model)).then((sentModel) =>
        streamCodeAgent(
          {
            dataset_columns: parsedDataset?.columns ?? [],
            column_roles: columnRoles,
            column_kinds: imageColumnKinds,
            sample_rows: sampleRows,
            user_message: userMessage,
            chat_history: chatHistory,
            prior_signature: snapshot.signatureCode,
            prior_metric: snapshot.metricCode,
            prior_signature_validation: summarizeValidation(snapshot.signatureValidation),
            prior_metric_validation: summarizeValidation(snapshot.metricValidation),
            initial_signature: initialSignature,
            initial_metric: initialMetric,
            locale: getActiveLocale(),
            ...(interviewBrief && interviewBrief.length > 0
              ? { interview_brief: interviewBrief }
              : {}),
            ...(isWorkflow
              ? {
                  prior_workflow: priorWorkflow,
                  initial_workflow: initialWorkflowRef.current ?? priorWorkflow,
                }
              : {}),
            ...(sentModel ? { model: sentModel } : {}),
            ...effortField(sentModel),
            ...(unified ? { unified } : {}),
            ...(unified && Object.keys(latestFiles).length > 0 ? { prior_files: latestFiles } : {}),
            ...(steerKey ? { steer_key: steerKey } : {}),
          },
          {
            signal: controller.signal,
            onSteerApplied: ({ ids, text }) => {
              if (controller.signal.aborted) return;
              steerStore.applied(ids);
              if (!text.trim()) return;
              replyBeforeSteer = replyBufRef.current;
              // The reply so far stays as its own bubble; the rest of the turn
              // streams into a new one after the user's message.
              setMessages((prev) => {
                const last = prev[prev.length - 1];
                const kept =
                  last && last.role === "assistant" && !last.content && !last.toolCalls?.length
                    ? prev.slice(0, -1)
                    : prev;
                return [
                  ...kept,
                  { role: "user", content: text, createdAt: Date.now() },
                  { role: "assistant", content: "", toolCalls: [] },
                ];
              });
            },
            onReasoningPatch: (chunk, source) => {
              if (reasoningBufRef.current === "") {
                setReasoningStartedAt(Date.now());
              }
              const sections = reasoningSectionsRef.current;
              if (!(source in sections.bufs)) {
                sections.order.push(source);
                sections.bufs[source] = "";
                reasoningLanesRef.current.add(source);
                if (reasoningEndedRef.current) {
                  reasoningEndedRef.current = false;
                  setReasoningEndedAt(null);
                }
                if (source === "metric") {
                  setMetricStatus((prev) => (prev === "waiting" ? "writing" : prev));
                } else if (source === "signature") {
                  setSignatureStatus((prev) => (prev === "waiting" ? "writing" : prev));
                }
              }
              sections.bufs[source] += chunk;
              reasoningBufRef.current = composeReasoning(sections, reasoningLanesRef.current);
              setReasoning(reasoningBufRef.current);
            },
            onSignaturePatch: (chunk) => {
              if (sigBufRef.current === "") {
                setStatusLabel(
                  unified
                    ? unifiedMsg("submit.v2.metric.agent.writing_run")
                    : msg("auto.features.submit.hooks.use.code.agent.literal.2"),
                );
                setSignatureStatus("writing");
                markReasoningDone("signature");
              }
              markFlowing("signature", true);
              sigBufRef.current += chunk;
              latestSignature = sigBufRef.current;
              setSignatureCode(sigBufRef.current);
              setSignatureValidation(null);
            },
            onMetricPatch: (chunk) => {
              if (metricBufRef.current === "") {
                setStatusLabel(
                  unified
                    ? unifiedMsg("submit.v2.metric.agent.writing_metric")
                    : msg("auto.features.submit.hooks.use.code.agent.literal.3"),
                );
                // The unified seed's run code finishes on its own `artifact_done`.
                if (!unified) setSignatureStatus("done");
                setMetricStatus("writing");
                markReasoningDone("metric");
              }
              markFlowing("metric", true);
              metricBufRef.current += chunk;
              latestMetric = metricBufRef.current;
              setMetricCode(metricBufRef.current);
              setMetricValidation(null);
            },
            onArtifactDone: (source) => {
              if (controller.signal.aborted) return;
              markReasoningDone(source);
              if (source === "signature" || source === "metric") markFlowing(source, false);
              // The seed's turn only ends after the intro message is written,
              // seconds later; landing the lane's code now keeps its "Edited"
              // row in the transcript instead of a gap until then.
              if (source === "signature") {
                runLaneDone = true;
                pushSignatureVersion(latestSignature);
                setSignatureStatus("done");
                if (!metricLaneDone) {
                  setStatusLabel(unifiedMsg("submit.v2.metric.agent.writing_metric"));
                }
              } else if (source === "metric") {
                metricLaneDone = true;
                pushMetricVersion(latestMetric);
                setMetricStatus("done");
                if (!runLaneDone) {
                  setStatusLabel(unifiedMsg("submit.v2.metric.agent.writing_run"));
                }
              }
            },
            onMessagePatch: (chunk) => {
              if (replyBufRef.current === "") {
                setStatusLabel(msg("auto.features.submit.hooks.use.code.agent.literal.4"));
                markReasoningDone();
              }
              replyBufRef.current += chunk;
              appendReply(chunk);
            },
            onAsk: (ask) => {
              if (controller.signal.aborted) return;
              setQuestion(ask.options.length > 0 ? ask : null);
            },
            onBrief: (fields) => {
              if (controller.signal.aborted) return;
              onBriefRef.current?.(fields);
            },
            onToolStart: (ev) => {
              markReasoningDone();
              if (ev.tool !== "open_file") setStatusLabel(
                ev.tool === "verify_code"
                  ? msg("agent.coding_tool.check.checking")
                  : ev.tool === "write_file" || ev.tool === "delete_file"
                    ? formatMsg("agent.coding_tool.editing", { file: ev.path ?? "" })
                    : ev.tool === "set_brief"
                  ? unifiedMsg("submit.v2.metric.agent.writing_brief")
                  : isSeedTool(ev.tool)
                      ? unified
                        ? unifiedMsg("submit.v2.metric.agent.writing_run")
                        : msg("auto.features.submit.hooks.use.code.agent.literal.5")
                      : isScorerTool(ev.tool)
                        ? unified
                          ? unifiedMsg("submit.v2.metric.agent.writing_metric")
                          : msg("auto.features.submit.hooks.use.code.agent.literal.6")
                        : msg("workflow.agent.editing_graph"),
              );
              if (isSeedTool(ev.tool)) setSignatureStatus("writing");
              else if (isScorerTool(ev.tool)) setMetricStatus("writing");
              else if (ev.tool === "write_file" && ev.path) {
                const path = ev.path;
                setWritingFiles((prev) => (prev.includes(path) ? prev : [...prev, path]));
              }
              pushToolCall({
                id: ev.id,
                tool: ev.tool,
                reason: ev.reason,
                status: "running",
                startedAt: Date.now(),
                endedAt: null,
                ...(ev.path && { path: ev.path }),
              });
            },
            onCodeDraft: ({ source, chunk, reset, path }) => {
              if (controller.signal.aborted) return;
              if (source === "file" && path) {
                if (!fileBefore.has(path)) fileBefore.set(path, latestFiles[path] ?? null);
                const text = (reset ? "" : (fileDraft.get(path) ?? "")) + chunk;
                fileDraft.set(path, text);
                setFileRef.current?.(path, text);
                setWritingFiles((prev) => (prev.includes(path) ? prev : [...prev, path]));
              } else if (source === "signature") {
                setSignatureCode(drafts.write(source, chunk, reset));
                setSignatureStatus("writing");
                markFlowing("signature", true);
              } else if (source === "metric") {
                setMetricCode(drafts.write(source, chunk, reset));
                setMetricStatus("writing");
                markFlowing("metric", true);
              }
            },
            onFileReplace: (path, code) => {
              if (controller.signal.aborted) return;
              markReasoningDone();
              fileBefore.delete(path);
              fileDraft.delete(path);
              const prevCode = latestFiles[path] ?? "";
              latestFiles[path] = code;
              setFileRef.current?.(path, code);
              // A file the user wrote or cloned is the base the agent's first
              // version diffs against; stamped 0 so it is never counted as an edit.
              if (prevCode) pushFileVersion(path, prevCode, 0);
              pushFileVersion(path, code);
              setMessages((prev) => {
                const last = prev[prev.length - 1];
                if (!last || last.role !== "assistant" || !last.toolCalls?.length) return prev;
                const next = prev.slice();
                next[next.length - 1] = {
                  ...last,
                  toolCalls: last.toolCalls.map((t) =>
                    t.tool === "write_file" && t.path === path && t.status === "running"
                      ? { ...t, prevCode, newCode: code }
                      : t,
                  ),
                };
                return next;
              });
            },
            onFileDelete: (path) => {
              if (controller.signal.aborted) return;
              delete latestFiles[path];
              fileBefore.delete(path);
              fileDraft.delete(path);
              setFileRef.current?.(path, null);
            },
            onVerifyResult: (result) => {
              if (controller.signal.aborted) return;
              setVerification(result);
              updateToolCall(result.id, { verification: result });
            },
            onToolEnd: (ev) => {
              if (ev.status !== "ok") {
                if (isSeedTool(ev.tool)) dropDraft("signature");
                else if (isScorerTool(ev.tool)) dropDraft("metric");
                else if (ev.tool === "write_file" && ev.path) dropFileDraft(ev.path);
              }
              if (ev.tool === "write_file" && ev.path) stopWriting(ev.path);
              if (ev.tool === "open_file" && ev.status === "ok" && ev.path && !controller.signal.aborted) {
                if (ev.line) updateToolCall(ev.id, { line: ev.line });
                onOpenFileRef.current?.(ev.path, ev.line);
              }
              finishToolCall(ev.id, ev.status === "ok" ? "done" : "error");
              if (isSeedTool(ev.tool)) {
                setSignatureStatus("done");
                markFlowing("signature", false);
              } else if (isScorerTool(ev.tool)) {
                setMetricStatus("done");
                markFlowing("metric", false);
              }
            },
            onWorkflowReplace: (workflow, changedNodeId) => {
              markReasoningDone();
              applyAgentWorkflowRef.current?.(workflow, changedNodeId);
            },
            onSignatureReplace: (code) => {
              drafts.land("signature");
              const prevCode = lastSignatureCode;
              lastSignatureCode = code;
              latestSignature = code;
              setSignatureCode(code);
              setSignatureManuallyEdited(false);
              attachCodeToLatestRunningToolCall("seed", prevCode, code);
              const changed = diffChangedLines(prevCode, code);
              setSignatureFlashLines(changed);
              if (flashClearRef.current) clearTimeout(flashClearRef.current);
              flashClearRef.current = setTimeout(() => {
                setSignatureFlashLines([]);
              }, 1200);
              pushSignatureVersion(code);
              pendingValidationsRef.current.push({
                kind: "signature",
                promise: runnersRef.current.runSignatureValidation(code),
              });
            },
            onMetricReplace: (code) => {
              drafts.land("metric");
              const prevCode = lastMetricCode;
              lastMetricCode = code;
              latestMetric = code;
              setMetricCode(code);
              setMetricManuallyEdited(false);
              attachCodeToLatestRunningToolCall("scorer", prevCode, code);
              const changed = diffChangedLines(prevCode, code);
              setMetricFlashLines(changed);
              if (flashClearRef.current) clearTimeout(flashClearRef.current);
              flashClearRef.current = setTimeout(() => {
                setMetricFlashLines([]);
              }, 1200);
              pushMetricVersion(code);
              pendingValidationsRef.current.push({
                kind: "metric",
                promise: runnersRef.current.runMetricValidation(code),
              });
            },
            onDone: async (result) => {
              // A superseded/reset run's late completion must not clobber the
              // fresh session (mirrors the onError guard below). The check at
              // the auto-fix step still matters — abort can land while the
              // validations above are awaited.
              if (controller.signal.aborted) return;
              dropDraft("signature");
              dropDraft("metric");
              dropFileDrafts();
              if (result.files) {
                // The server's files after the turn are the truth; the events
                // above already landed them, so this only mends a missed one.
                for (const [path, code] of Object.entries(result.files)) {
                  if (latestFiles[path] !== code) setFileRef.current?.(path, code);
                }
                for (const path of Object.keys(latestFiles)) {
                  if (!(path in result.files)) setFileRef.current?.(path, null);
                }
              }
              if (result.verification !== undefined && result.verification !== null) {
                setVerification(result.verification);
              }
              // A chat turn that ends without a word is a failed turn, not a
              // finished one: the chat offers a retry instead of a bare "Done.".
              if (isChat && !result.assistant_message.trim() && !replyBufRef.current.trim()) {
                setStatus("error");
                setStatusLabel(msg("auto.features.submit.hooks.use.code.agent.literal.8"));
                setSignatureStatus("idle");
                setMetricStatus("idle");
                setError(null);
                if (reasoningBufRef.current) setReasoningEndedAt(Date.now());
                setMessages((prev) => {
                  const last = prev[prev.length - 1];
                  if (last && last.role === "assistant" && !last.toolCalls?.length) {
                    return prev.slice(0, -1);
                  }
                  return prev;
                });
                return;
              }
              if (!isChat && isWorkflow) {
                // Workflow seed: the graph landed via workflow_replace (or
                // rides the done payload after a repair); only the metric
                // flows through the code editor.
                if (result.workflow) {
                  applyAgentWorkflowRef.current?.(result.workflow, null);
                }
                setMetricCode(result.metric_code);
                setMetricManuallyEdited(false);
                pushMetricVersion(result.metric_code);
                pendingValidationsRef.current.push({
                  kind: "metric",
                  promise: runnersRef.current.runMetricValidation(result.metric_code),
                });
              } else if (!isChat) {
                setSignatureCode(result.signature_code);
                setMetricCode(result.metric_code);
                setSignatureManuallyEdited(false);
                setMetricManuallyEdited(false);
                const sigChanged = diffChangedLines(snapshot.signatureCode, result.signature_code);
                const metChanged = diffChangedLines(snapshot.metricCode, result.metric_code);
                setSignatureFlashLines(sigChanged);
                setMetricFlashLines(metChanged);
                if (flashClearRef.current) clearTimeout(flashClearRef.current);
                flashClearRef.current = setTimeout(() => {
                  setSignatureFlashLines([]);
                  setMetricFlashLines([]);
                }, 800);
                pushSignatureVersion(result.signature_code);
                pushMetricVersion(result.metric_code);
                pendingValidationsRef.current.push({
                  kind: "signature",
                  promise: runnersRef.current.runSignatureValidation(result.signature_code),
                });
                pendingValidationsRef.current.push({
                  kind: "metric",
                  promise: runnersRef.current.runMetricValidation(result.metric_code),
                });
              }
              setSignatureStatus("done");
              setMetricStatus("done");
              setStatus("done");
              setStatusLabel(msg("auto.features.submit.hooks.use.code.agent.literal.7"));
              if (reasoningBufRef.current) setReasoningEndedAt(Date.now());

              setMessages((prev) => {
                const last = prev[prev.length - 1];
                if (!last || last.role !== "assistant") return prev;
                const fallback = isChat
                  ? ""
                  : unified
                    ? unifiedMsg("submit.v2.metric.agent.seed_done")
                    : "I wrote a Signature and Metric based on your data.";
                let replyTail = result.assistant_message;
                if (replyBeforeSteer !== null) {
                  replyTail = replyTail.startsWith(replyBeforeSteer)
                    ? replyTail.slice(replyBeforeSteer.length).trimStart()
                    : last.content;
                  // Nothing came after the steer: no hollow bubble.
                  if (!replyTail && !last.content && !last.toolCalls?.length) return prev.slice(0, -1);
                }
                const finalContent = replyTail || last.content || fallback;
                const next = prev.slice();
                next[next.length - 1] = {
                  ...last,
                  content: finalContent,
                  model: result.model,
                  servedModel: result.served_model,
                  stats: result.stats,
                  createdAt: Date.now(),
                };
                return next;
              });

              // Auto-fix: wait for any validations kicked off during this run,
              // then if the validator flagged errors, fire a follow-up chat
              // turn so the agent can see the errors (via prior_*_validation)
              // and patch them. Bounded by MAX_AUTO_FIX to prevent loops.
              const pending = pendingValidationsRef.current;
              pendingValidationsRef.current = [];
              if (pending.length === 0) return;
              // allSettled so a single failed validation doesn't suppress
              // auto-fix for the other kind. A rejection here means the
              // validation request itself errored (network/server) — there
              // is nothing concrete for the agent to patch in that case, so
              // we skip just that entry rather than aborting the whole run.
              const settled = await Promise.allSettled(pending.map((e) => e.promise));
              const responses: Array<ValidateCodeResponse | null> = settled.map((r) =>
                r.status === "fulfilled" ? ((r.value as ValidateCodeResponse | null) ?? null) : null,
              );
              for (let i = 0; i < pending.length; i++) {
                const entry = pending[i];
                if (!entry) continue;
                const resp = responses[i] ?? null;
                if (entry.kind === "signature") {
                  snapshotRef.current = {
                    ...snapshotRef.current,
                    signatureValidation: resp,
                  };
                } else {
                  snapshotRef.current = {
                    ...snapshotRef.current,
                    metricValidation: resp,
                  };
                }
              }
              let signatureHasError = false;
              let metricHasError = false;
              for (let i = 0; i < pending.length; i++) {
                const entry = pending[i];
                const resp = responses[i];
                const broken = !!resp && !resp.valid && resp.errors.length > 0;
                if (!entry || !broken) continue;
                if (entry.kind === "signature") signatureHasError = true;
                else metricHasError = true;
              }
              if (!signatureHasError && !metricHasError) return;
              if (controller.signal.aborted) return;
              if (autoFixAttemptsRef.current >= MAX_AUTO_FIX) return;
              autoFixAttemptsRef.current += 1;
              // Outside the unified contract only the workflow canvas authors,
              // and there the graph is checked on the canvas: only the metric
              // can fail validation. The request body carries the concrete errors.
              const fixMessage = unified
                ? unifiedMsg("submit.v2.metric.agent.fix")
                : msg("auto.features.submit.hooks.use.code.agent.template.5");
              queueMicrotask(() => {
                runAgentRef.current?.(fixMessage, messagesRef.current);
              });
            },
            onError: (message, code) => {
              if (controller.signal.aborted) return;
              dropDraft("signature");
              dropDraft("metric");
              setLimitReached(code === DAILY_LIMIT_CODE);
              setStatus("error");
              setStatusLabel(msg("auto.features.submit.hooks.use.code.agent.literal.8"));
              setSignatureStatus("idle");
              setMetricStatus("idle");
              setError(message);
              // Drop the empty placeholder agent bubble so the error UI
              // isn't followed by a blank message.
              setMessages((prev) => {
                const last = prev[prev.length - 1];
                if (last && last.role === "assistant" && !last.content && !last.toolCalls?.length) {
                  return prev.slice(0, -1);
                }
                return prev;
              });
            },
          },
      ),
      );
    },
    [
      parsedDataset,
      hasRequiredContext,
      canChat,
      columnRoles,
      columnKinds,
      isWorkflow,
      interviewBrief,
      unified,
      model,
      resolveModel,
      reasoningEffort,
      resolveEffort,
      setSignatureCode,
      setMetricCode,
      setSignatureValidation,
      setMetricValidation,
      setSignatureManuallyEdited,
      setMetricManuallyEdited,
      appendReply,
      pushToolCall,
      finishToolCall,
      attachCodeToLatestRunningToolCall,
      pushSignatureVersion,
      pushMetricVersion,
      pushFileVersion,
      updateToolCall,
      steerStore,
      markFlowing,
    ],
  );

  React.useEffect(() => {
    runAgentRef.current = runAgent;
  }, [runAgent]);

  const send = React.useCallback(
    (message: string, options?: { hidden?: boolean }) => {
      const trimmed = message.trim();
      if (!trimmed) return;
      autoFixAttemptsRef.current = 0;
      runAgent(trimmed, messages, options?.hidden ?? false);
    },
    [runAgent, messages],
  );
  React.useEffect(() => {
    sendLatestRef.current = send;
  }, [send]);

  const editAndResend = React.useCallback(
    (messageIndex: number, content: string) => {
      const trimmed = content.trim();
      if (!trimmed) return;
      const truncated = messages.slice(0, messageIndex);
      setMessages(truncated);
      autoFixAttemptsRef.current = 0;
      runAgent(trimmed, truncated);
    },
    [runAgent, messages],
  );

  const retry = React.useCallback(() => {
    const lastUserIndex = [...messages].reverse().findIndex((m) => m.role === "user");
    if (lastUserIndex === -1) return;
    const index = messages.length - 1 - lastUserIndex;
    const lastUser = messages[index];
    const truncated = messages.slice(0, index);
    setMessages(truncated);
    autoFixAttemptsRef.current = 0;
    runAgent(lastUser?.content ?? "", truncated, lastUser?.hidden ?? false);
  }, [messages, runAgent]);

  const goToSignatureVersion = React.useCallback(
    (index: number) => {
      const v = signatureVersions[index];
      if (!v) return;
      setSignatureCode(v.code);
      setSignatureManuallyEdited(false);
      setSignatureVersionIndex(index);
      void runnersRef.current.runSignatureValidation(v.code);
    },
    [signatureVersions, setSignatureCode, setSignatureManuallyEdited],
  );

  const goToMetricVersion = React.useCallback(
    (index: number) => {
      const v = metricVersions[index];
      if (!v) return;
      setMetricCode(v.code);
      setMetricManuallyEdited(false);
      setMetricVersionIndex(index);
      void runnersRef.current.runMetricValidation(v.code);
    },
    [metricVersions, setMetricCode, setMetricManuallyEdited],
  );

  const stop = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStatus("idle");
    setStatusLabel("");
    setSignatureStatus("idle");
    setMetricStatus("idle");
  }, []);

  const reset = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    sigBufRef.current = "";
    metricBufRef.current = "";
    replyBufRef.current = "";
    reasoningBufRef.current = "";
    reasoningSectionsRef.current = { order: [], bufs: {} };
    reasoningEndedRef.current = false;
    pendingValidationsRef.current = [];
    autoFixAttemptsRef.current = 0;
    autoRanRef.current = false;
    steerStore.clear();
    if (flashClearRef.current) {
      clearTimeout(flashClearRef.current);
      flashClearRef.current = null;
    }
    setMessages([]);
    setQuestion(null);
    setStatus("idle");
    setLimitReached(false);
    setMode("seed");
    setStatusLabel("");
    setSignatureStatus("idle");
    setMetricStatus("idle");
    setError(null);
    setReasoning("");
    setReasoningStartedAt(null);
    setReasoningEndedAt(null);
    setSignatureVersions([]);
    setMetricVersions([]);
    setFileVersions({});
    setWritingFiles([]);
    setVerification(null);
    setSignatureVersionIndex(-1);
    setMetricVersionIndex(-1);
    setSignatureFlashLines([]);
    setMetricFlashLines([]);
    initialWorkflowRef.current = null;
    // Clear the manual-edit gate so the auto-seed effect re-fires on
    // sessionKey bump — otherwise "new chat" would wipe the messages
    // but leave the old Signature/Metric untouched.
    setSignatureManuallyEdited(false);
    setMetricManuallyEdited(false);
    setSessionKey((k) => k + 1);
  }, [setSignatureManuallyEdited, setMetricManuallyEdited, steerStore]);

  // Swapping the dataset invalidates the entire conversation: messages,
  // version history, and any in-flight stream all refer to schemas that no
  // longer apply. Identity change is a reliable signal — `parseDatasetFile`
  // returns a fresh object per upload. Skipping the initial null→value
  // transition keeps the first mount a no-op.
  const prevDatasetRef = React.useRef(parsedDataset);
  React.useEffect(() => {
    if (prevDatasetRef.current !== parsedDataset) {
      if (prevDatasetRef.current !== null) {
        reset();
      } else {
        autoRanRef.current = false;
      }
      prevDatasetRef.current = parsedDataset;
    }
  }, [parsedDataset, reset]);

  // Re-arm when the user changes their DSPy module (predict ↔ chain_of_thought
  // ↔ react, …). The manual-edit gate still protects user-authored edits;
  // fresh seed output just flips to the new module's expected shape.
  React.useEffect(() => {
    autoRanRef.current = false;
  }, [moduleName]);

  // Kick off the seed run as soon as the user has a dataset + I/O roles.
  // This hook lives at the wizard level, so the seed fires even when the
  // user hasn't arrived at the code step yet — by the time they do, the
  // editors are already filled (or actively filling). We skip the seed
  // if either artifact has been manually authored (including clone-pre-fill,
  // which marks both flags true) — a fresh dataset upload clears the flags.
  React.useEffect(() => {
    if (!seedEnabled) return;
    if (autoRanRef.current) return;
    if (!hasRequiredContext) return;
    // Workflow mode waits for the wizard's starter graph (the request needs
    // a prior_workflow to route the graph-aware path) and respects canvas
    // edits the way the code path respects manual code edits.
    if (isWorkflow && (!workflowSpec || workflowTouched || metricManuallyEdited)) return;
    if (!isWorkflow && (signatureManuallyEdited || metricManuallyEdited)) return;
    autoRanRef.current = true;
    autoFixAttemptsRef.current = 0;
    runAgent("", []);
  }, [
    seedEnabled,
    hasRequiredContext,
    signatureManuallyEdited,
    metricManuallyEdited,
    isWorkflow,
    workflowSpec,
    workflowTouched,
    runAgent,
    sessionKey,
  ]);

  return {
    status,
    mode,
    statusLabel,
    signatureStatus,
    metricStatus,
    signatureFlowing: flowing.signature,
    metricFlowing: flowing.metric,
    messages,
    error,
    limitReached,
    canSend: canChat && status !== "streaming",
    question,
    signatureVersions,
    metricVersions,
    fileVersions,
    writingFiles,
    verification,
    signatureVersionIndex,
    metricVersionIndex,
    signatureFlashLines,
    metricFlashLines,
    reasoning,
    reasoningStartedAt,
    reasoningEndedAt,
    goToSignatureVersion,
    goToMetricVersion,
    send,
    midTurn,
    editAndResend,
    retry,
    stop,
    reset,
  };
}
