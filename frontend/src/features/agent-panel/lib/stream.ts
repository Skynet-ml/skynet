import type {
  ApprovalResolvedPayload,
  ChatTurn,
  PendingApprovalPayload,
  ToolEndPayload,
  ToolStartPayload,
  TrustMode,
  WizardState,
} from "./types";
import { formatMsg, msg } from "@/shared/lib/messages";
import { getRuntimeEnv } from "@/shared/lib/runtime-env";
import { readServerSentEvents } from "@/shared/lib/sse";
import { dispatchGeneralistEvent } from "./stream-events";
import { announceWalletChange, apiUrl, fetchWithAuthRetry } from "@/shared/lib/api";
import type { ApiSource } from "@/shared/lib/api-route";

// Resolve lazily — a module-load const races the injected window.__APP_ENV__
// and freezes the build-time localhost fallback. See shared/lib/api.ts.
const apiBase = () => getRuntimeEnv().apiUrl;

/** The agent route on `source`; hosted goes through the linked Mac's proxy. */
function agentUrl(path: string, source: ApiSource): string {
  return source === "hosted" ? apiUrl(path, "hosted") : `${apiBase()}${path}`;
}

export interface GeneralistAgentRequest {
  user_message: string;
  chat_history: ChatTurn[];
  wizard_state: WizardState;
  trust_mode: TrustMode;
  conversation_id?: string | null;
  regenerate?: boolean;
  /** Original text of the user turn this message edits; its stored copy and every later row are replaced. */
  edit_user_text?: string;
  /** How many earlier chat user turns have the same text as the edited one. */
  edit_user_occurrence?: number;
  locale?: string;
  /** LiteLLM id of the catalog model to run the turn on; absent = default. */
  model?: string;
  /** Reasoning-effort level for the chosen model; absent = its default. */
  reasoning_effort?: string;
  /** Fast mode for a local CLI model that takes it; the backend drops it elsewhere. */
  fast_mode?: boolean;
  /** Client-generated id of this turn; `/agent-steer` messages posted with
   *  it are delivered into the running turn. */
  steer_key?: string;
  /** False runs the turn without saving it as a conversation; absent saves it. */
  persist?: boolean;
  /** The summary that stands in for the turns before `chat_history`, after a compaction. */
  compaction_summary?: string | null;
}

interface ConversationMetaPayload {
  conversation_id: string;
  title: string;
  /** The saved row of the turn's user message. */
  user_message_id?: number;
}

export interface GeneralistAgentHandlers {
  onReasoningPatch?: (chunk: string) => void;
  /** A one- or two-sentence label for the turn's `index`-th stretch of reasoning. */
  onThinkingSummary?: (ev: { index: number; text: string }) => void;
  /** What the agent said it will do this turn. */
  onPlan?: (text: string) => void;
  /** The model is still writing a call to `tool`; its `tool_start` follows once the call runs. */
  onToolPending?: (ev: { tool: string }) => void;
  /** A new model step started; pending rows with no `tool_start` yet never run. */
  onToolPendingClear?: () => void;
  onToolStart?: (ev: ToolStartPayload) => void;
  onToolEnd?: (ev: ToolEndPayload) => void;
  onPendingApproval?: (ev: PendingApprovalPayload) => void;
  onApprovalResolved?: (ev: ApprovalResolvedPayload) => void;
  onMessagePatch?: (chunk: string) => void;
  /** The reply streamed so far prefaced a tool call and is not the reply. */
  onMessageReset?: () => void;
  onConversationMeta?: (ev: ConversationMetaPayload) => void;
  /** A model title for a new chat; apply it only while its row still shows `cut_title`. */
  onConversationTitle?: (ev: ConversationMetaPayload & { cut_title: string }) => void;
  /** The agent read these steered messages at a step boundary; `text` joins them. */
  onSteerApplied?: (ev: { ids: string[]; text: string; message_id?: number }) => void;
  onDone: (result: {
    assistant_message: string;
    model: string | null;
    served_model: string | null;
    /** Raw `stats` block; read it with `parseTurnStats`. */
    stats: unknown;
  }) => void;
  /** The backend started to condense older turns before the reply. */
  onCompactionStart?: (ev: { reason: string; summarizedTurns: number | null }) => void;
  /** The compaction entry: read it with `parseCompaction`. */
  onCompaction?: (raw: unknown) => void;
  /** The summary call failed; the turn runs on without it. `cents` is the failed call's cost. */
  onCompactionError?: (ev: { cents: number | null }) => void;
  /** The turn's final `stats`, sent after `done` when later model calls changed its cost. */
  onTurnStats?: (stats: unknown) => void;
  onError: (message: string, code?: string) => void;
  signal?: AbortSignal;
}

/** What `/compact` sends: the chat window to condense and what to focus on. */
export interface CompactChatRequest {
  chat_history: ChatTurn[];
  compaction_summary?: string | null;
  instructions?: string;
  conversation_id?: string | null;
  model?: string;
  reasoning_effort?: string;
  fast_mode?: boolean;
}

/**
 * Stream generalist-agent events via SSE. `source` "hosted" runs the turn on
 * the linked web account; `req.conversation_id` is then that account's id.
 */
export function streamGeneralistAgent(
  req: GeneralistAgentRequest,
  handlers: GeneralistAgentHandlers,
  source: ApiSource = "local",
): Promise<void> {
  return streamAgentRoute("/optimizations/generalist-agent", req, handlers, source);
}

/** Condense a chat's older turns now (`/compact`); the events are the turn's compaction events. */
export function streamCompaction(
  req: CompactChatRequest,
  handlers: GeneralistAgentHandlers,
  source: ApiSource = "local",
): Promise<void> {
  return streamAgentRoute("/optimizations/generalist-agent/compact", req, handlers, source);
}

async function streamAgentRoute(
  path: string,
  req: object,
  handlers: GeneralistAgentHandlers,
  source: ApiSource,
): Promise<void> {
  let res: Response;
  try {
    res = await fetchWithAuthRetry(agentUrl(path, source), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(req),
      signal: handlers.signal,
    });
  } catch (err) {
    if ((err as Error)?.name === "AbortError") return;
    handlers.onError(msg("auto.features.agent.panel.lib.stream.literal.1"));
    return;
  }
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    let detail: string | undefined;
    let code: string | undefined;
    try {
      const body = JSON.parse(text);
      const raw = body.detail;
      detail = typeof raw === "string" ? raw : raw != null ? JSON.stringify(raw) : undefined;
      code = typeof body.code === "string" ? body.code : undefined;
    } catch {
      /* not json */
    }
    handlers.onError(
      detail ?? formatMsg("auto.features.agent.panel.lib.stream.template.1", { p1: res.status }),
      code,
    );
    return;
  }
  const processEvent = ({ event, data }: { event: string; data: Record<string, unknown> }) => {
    dispatchGeneralistEvent(handlers, event, data, () =>
      msg("auto.features.agent.panel.lib.stream.literal.2"),
    );
    // The stream can stay open for the chat title after the turn. The server
    // sends ``turn_billed`` once the debit ran, so the balance needs no wait.
    if (event === "turn_billed") announceWalletChange();
  };
  try {
    await readServerSentEvents(res.body, processEvent);
  } catch (err) {
    if ((err as Error)?.name !== "AbortError") {
      handlers.onError(
        err instanceof Error ? err.message : msg("auto.features.agent.panel.lib.stream.literal.3"),
      );
    }
  } finally {
    // The turn is debited as its stream closes on the server.
    announceWalletChange();
  }
}

/** Resolve a pending approval via the companion confirm endpoint. */
export async function confirmGeneralistApproval(
  callId: string,
  approved: boolean,
  source: ApiSource = "local",
): Promise<boolean> {
  let res: Response;
  try {
    res = await fetchWithAuthRetry(agentUrl("/optimizations/generalist-agent/confirm", source), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ call_id: callId, approved }),
    });
  } catch {
    return false;
  }
  if (!res.ok) return false;
  const data = (await res.json().catch(() => ({ resolved: false }))) as { resolved?: boolean };
  return Boolean(data.resolved);
}
