// CRUD client for persisted agent conversations. Backs the history drawer.
//
// The conversation/messages persistence is best-effort on the server: a 401 or
// 5xx must not break the panel — the panel falls back to ephemeral mode and
// the user can keep talking. So every helper here returns a typed value on
// success and ``null`` on any failure, and the caller decides whether the
// missing data is fatal (drawer empty) or silent (no-op).

import type { AgentMessage, AgentToolCall } from "@/shared/ui/agent/types";
import { parseMessageTime } from "@/shared/ui/agent/message-time";
import { parseTurnStats } from "@/shared/ui/agent/turn-stats";
import { parseThinkingRows } from "@/shared/lib/thinking-segments";
import { getRuntimeEnv } from "@/shared/lib/runtime-env";
import { apiUrl, fetchWithAuthRetry, hostedRunsAvailable, webPicked } from "@/shared/lib/api";
import { parseSourceKey, sourceKey } from "@/shared/lib/api-route";
import type { ApiSource } from "@/shared/lib/api-route";
import { parseCompaction } from "./compaction";

// Resolve lazily — a module-load const races the injected window.__APP_ENV__
// and freezes the build-time localhost fallback. See shared/lib/api.ts.
const apiBase = () => getRuntimeEnv().apiUrl;

export interface ConversationSummary {
  id: string;
  title: string;
  pinned: boolean;
  archivedAt: string | null;
  createdAt: string;
  updatedAt: string;
  preview: string | null;
  /** "hosted" for a chat on the linked web account; its id is a hosted key. */
  source?: ApiSource;
}

export interface ConversationDetail extends ConversationSummary {
  messages: AgentMessage[];
}

interface RawSummary {
  id: string;
  title: string;
  pinned: boolean;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  preview?: string | null;
}

interface RawMessage {
  role: string;
  content: string;
  tool_calls?: Array<Record<string, unknown>> | null;
  model?: string | null;
  served_model?: string | null;
  stats?: Record<string, unknown> | null;
  plan?: string | null;
  thinking?: unknown;
  compaction?: unknown;
  side_cents?: number | null;
  id?: number | null;
  created_at: string;
}

interface RawDetail extends RawSummary {
  messages: RawMessage[];
}

function toSummary(raw: RawSummary, source: ApiSource = "local"): ConversationSummary {
  return {
    id: sourceKey(source, raw.id),
    ...(source === "hosted" ? { source } : {}),
    title: raw.title,
    pinned: Boolean(raw.pinned),
    archivedAt: raw.archived_at,
    createdAt: raw.created_at,
    updatedAt: raw.updated_at,
    preview: raw.preview ?? null,
  };
}

function toMessage(raw: RawMessage): AgentMessage {
  const toolCalls = Array.isArray(raw.tool_calls)
    ? raw.tool_calls.map(rawToToolCall).filter((tc): tc is AgentToolCall => tc !== null)
    : undefined;
  const role: AgentMessage["role"] = raw.role === "assistant" ? "assistant" : "user";
  const compaction = parseCompaction(raw.compaction);
  return {
    role,
    content: raw.content,
    toolCalls: toolCalls && toolCalls.length > 0 ? toolCalls : undefined,
    model: raw.model ?? undefined,
    servedModel: raw.served_model ?? undefined,
    stats: parseTurnStats(raw.stats),
    plan: typeof raw.plan === "string" && raw.plan.trim() ? raw.plan : undefined,
    thinkingSegments: parseThinkingRows(raw.thinking),
    createdAt: parseMessageTime(raw.created_at),
    ...(compaction ? { compaction } : {}),
    ...(typeof raw.id === "number" ? { id: raw.id } : {}),
    ...(typeof raw.side_cents === "number" && raw.side_cents > 0 ? { sideCents: raw.side_cents } : {}),
  };
}

// Tool-call payloads are stored as the same JSON shape the frontend keeps in
// React state. We still defensively coerce because untrusted JSON can carry
// anything — drop entries that lack the minimum keys instead of trusting them.
function rawToToolCall(raw: Record<string, unknown>): AgentToolCall | null {
  if (typeof raw.id !== "string" || typeof raw.tool !== "string") return null;
  const status = raw.status === "done" || raw.status === "error" ? raw.status : "done";
  const startedAt = typeof raw.startedAt === "number" ? raw.startedAt : Date.now();
  const endedAt = typeof raw.endedAt === "number" ? raw.endedAt : null;
  const payload =
    raw.payload && typeof raw.payload === "object"
      ? (raw.payload as Record<string, unknown>)
      : undefined;
  return {
    id: raw.id,
    tool: raw.tool,
    reason: typeof raw.reason === "string" ? raw.reason : "",
    status,
    startedAt,
    endedAt,
    payload,
  };
}

export interface ListConversationsParams {
  q?: string;
  pinned?: boolean;
  limit?: number;
  offset?: number;
}

export async function listConversations(
  params?: ListConversationsParams,
  signal?: AbortSignal,
): Promise<ConversationSummary[] | null> {
  const q = new URLSearchParams();
  if (params?.q) q.set("q", params.q);
  if (params?.pinned !== undefined) q.set("pinned", String(params.pinned));
  if (params?.limit) q.set("limit", String(params.limit));
  if (params?.offset) q.set("offset", String(params.offset));
  const qs = q.toString();
  const path = `/agent/conversations${qs ? `?${qs}` : ""}`;
  // A linked Mac lists the side picked in the header.
  return webPicked()
    ? readSummaries(apiUrl(path, "hosted"), "hosted", signal)
    : readSummaries(`${apiBase()}${path}`, "local", signal);
}

/**
 * Read one conversation list from `url`, keyed for `source`.
 *
 * Args:
 *   url: The full list URL.
 *   source: Where the list comes from; hosted rows get hosted keys.
 *   signal: Aborts the read.
 *
 * Returns:
 *   The rows, or null on any failure.
 */
async function readSummaries(
  url: string,
  source: ApiSource,
  signal?: AbortSignal,
): Promise<ConversationSummary[] | null> {
  try {
    const res = await fetchWithAuthRetry(url, { method: "GET", signal });
    if (!res.ok) return null;
    const rows = (await res.json()) as RawSummary[];
    return rows.map((row) => toSummary(row, source));
  } catch {
    return null;
  }
}

/** Whether `conversationId` is a hosted key: a chat on the linked web account. */
export function isHostedConversation(conversationId: string): boolean {
  return parseSourceKey(conversationId).source === "hosted";
}

/** The URL of one conversation route, on the source its key names. */
function conversationUrl(conversationId: string, suffix = ""): string {
  const { source, id } = parseSourceKey(conversationId);
  const path = `/agent/conversations/${encodeURIComponent(id)}${suffix}`;
  return source === "hosted" ? apiUrl(path, "hosted") : `${apiBase()}${path}`;
}

export async function getConversation(
  conversationId: string,
  signal?: AbortSignal,
): Promise<ConversationDetail | null> {
  return (await loadConversation(conversationId, signal)).detail;
}

/**
 * Load one conversation. `missing` is true only when the server answers 404,
 * so a server error or no network does not count as a deleted chat.
 */
export async function loadConversation(
  conversationId: string,
  signal?: AbortSignal,
): Promise<{ detail: ConversationDetail | null; missing: boolean }> {
  const { source, id } = parseSourceKey(conversationId);
  // An unlinked Mac would send a web id to the local backend.
  if (source === "hosted" && !hostedRunsAvailable()) return { detail: null, missing: false };
  const path = `/agent/conversations/${encodeURIComponent(id)}`;
  try {
    const res = await fetchWithAuthRetry(
      source === "hosted" ? apiUrl(path, "hosted") : `${apiBase()}${path}`,
      { method: "GET", signal },
    );
    if (!res.ok) return { detail: null, missing: res.status === 404 };
    const raw = (await res.json()) as RawDetail;
    return {
      detail: { ...toSummary(raw, source), messages: raw.messages.map(toMessage) },
      missing: false,
    };
  } catch {
    return { detail: null, missing: false };
  }
}

export interface ConversationPatch {
  title?: string;
  pinned?: boolean;
}

export async function patchConversation(
  conversationId: string,
  patch: ConversationPatch,
): Promise<ConversationSummary | null> {
  if (isHostedConversation(conversationId) && !hostedRunsAvailable()) return null;
  try {
    const res = await fetchWithAuthRetry(conversationUrl(conversationId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    if (!res.ok) return null;
    const raw = (await res.json()) as RawSummary;
    return toSummary(raw, parseSourceKey(conversationId).source);
  } catch {
    return null;
  }
}

export async function deleteConversation(conversationId: string): Promise<boolean> {
  if (isHostedConversation(conversationId) && !hostedRunsAvailable()) return false;
  try {
    const res = await fetchWithAuthRetry(conversationUrl(conversationId), { method: "DELETE" });
    return res.ok;
  } catch {
    return false;
  }
}

/**
 * Save a new conversation with copies of the turns before one user turn.
 *
 * Args:
 *   conversationId: The source conversation key.
 *   userTurns: How many user turns, from the start, the fork keeps.
 *
 * Returns:
 *   The new conversation, keyed on the source's side, or null on any failure.
 */
export async function forkConversation(
  conversationId: string,
  userTurns: number,
): Promise<ConversationSummary | null> {
  if (isHostedConversation(conversationId) && !hostedRunsAvailable()) return null;
  try {
    const res = await fetchWithAuthRetry(conversationUrl(conversationId, "/fork"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_turns: userTurns }),
    });
    if (!res.ok) return null;
    const raw = (await res.json()) as RawSummary;
    return toSummary(raw, parseSourceKey(conversationId).source);
  } catch {
    return null;
  }
}
