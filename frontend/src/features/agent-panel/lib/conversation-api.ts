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
import { fetchWithAuthRetry } from "@/shared/lib/api";

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
  created_at: string;
}

interface RawDetail extends RawSummary {
  messages: RawMessage[];
}

function toSummary(raw: RawSummary): ConversationSummary {
  return {
    id: raw.id,
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
  try {
    const res = await fetchWithAuthRetry(`${apiBase()}/agent/conversations${qs ? `?${qs}` : ""}`, {
      method: "GET",
      signal,
    });
    if (!res.ok) return null;
    const rows = (await res.json()) as RawSummary[];
    return rows.map(toSummary);
  } catch {
    return null;
  }
}

export async function getConversation(
  conversationId: string,
  signal?: AbortSignal,
): Promise<ConversationDetail | null> {
  try {
    const res = await fetchWithAuthRetry(
      `${apiBase()}/agent/conversations/${encodeURIComponent(conversationId)}`,
      { method: "GET", signal },
    );
    if (!res.ok) return null;
    const raw = (await res.json()) as RawDetail;
    return {
      ...toSummary(raw),
      messages: raw.messages.map(toMessage),
    };
  } catch {
    return null;
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
  try {
    const res = await fetchWithAuthRetry(
      `${apiBase()}/agent/conversations/${encodeURIComponent(conversationId)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      },
    );
    if (!res.ok) return null;
    const raw = (await res.json()) as RawSummary;
    return toSummary(raw);
  } catch {
    return null;
  }
}

export async function deleteConversation(conversationId: string): Promise<boolean> {
  try {
    const res = await fetchWithAuthRetry(
      `${apiBase()}/agent/conversations/${encodeURIComponent(conversationId)}`,
      { method: "DELETE" },
    );
    return res.ok;
  } catch {
    return false;
  }
}

/** Create (or reuse) the conversation's public read-only link token; null on failure. */
export async function createConversationShare(conversationId: string): Promise<string | null> {
  try {
    const res = await fetchWithAuthRetry(
      `${apiBase()}/agent/conversations/${encodeURIComponent(conversationId)}/share`,
      { method: "POST" },
    );
    if (!res.ok) return null;
    const raw = (await res.json()) as { token?: unknown };
    return typeof raw.token === "string" && raw.token ? raw.token : null;
  } catch {
    return null;
  }
}

/** One of the user's shared chats, as the settings list shows it. */
export interface SharedConversationSummary {
  id: string;
  title: string;
  sharedAt: string | null;
  token: string;
}

/**
 * List the user's chats that have a live public link, newest share first. In
 * the Mac app the local backend reads the list from the linked hosted account.
 * Throws on a failed request so the caller can show an error.
 */
export async function listSharedConversations(): Promise<SharedConversationSummary[]> {
  const res = await fetchWithAuthRetry(`${apiBase()}/agent/shared-conversations`, { method: "GET" });
  if (!res.ok) throw new Error(`shared chats list failed (${res.status})`);
  const raw = (await res.json()) as Array<{ id: string; title: string; shared_at: string | null; token: string }>;
  return raw.map((r) => ({ id: r.id, title: r.title, sharedAt: r.shared_at, token: r.token }));
}

/** Stop sharing a chat: its public link then answers "not found". Returns false on failure. */
export async function revokeConversationShare(conversationId: string): Promise<boolean> {
  try {
    const res = await fetchWithAuthRetry(
      `${apiBase()}/agent/conversations/${encodeURIComponent(conversationId)}/share`,
      { method: "DELETE" },
    );
    return res.ok;
  } catch {
    return false;
  }
}

export interface SharedConversation {
  title: string;
  messages: AgentMessage[];
}

/**
 * Read a shared conversation by its link token. The route is public, so this
 * sends no credentials. Returns null for an unknown or revoked link and on any
 * network failure.
 */
export async function getSharedConversation(
  token: string,
  signal?: AbortSignal,
): Promise<SharedConversation | null> {
  try {
    const res = await fetch(
      `${apiBase()}/agent/shared-conversations/${encodeURIComponent(token)}`,
      { method: "GET", signal, credentials: "omit" },
    );
    if (!res.ok) return null;
    const raw = (await res.json()) as { title: string; messages: RawMessage[] };
    return { title: raw.title, messages: raw.messages.map(toMessage) };
  } catch {
    return null;
  }
}
