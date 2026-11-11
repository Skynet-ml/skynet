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
  const { source, id } = parseSourceKey(conversationId);
  // An unlinked Mac would send a web id to the local backend.
  if (source === "hosted" && !hostedRunsAvailable()) return null;
  const path = `/agent/conversations/${encodeURIComponent(id)}`;
  try {
    const res = await fetchWithAuthRetry(
      source === "hosted" ? apiUrl(path, "hosted") : `${apiBase()}${path}`,
      { method: "GET", signal },
    );
    if (!res.ok) return null;
    const raw = (await res.json()) as RawDetail;
    return {
      ...toSummary(raw, source),
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

/** The three kinds of chat share link (SKY-294). */
export type ShareLinkType = "view" | "continue" | "continue_data";

/** A run, dataset or file that a share link can grant read access to. */
export interface SharedItemOption {
  kind: "run" | "dataset" | "file";
  id: string;
  label: string;
  /** False when the item no longer exists. */
  available: boolean;
}

/** The owner's view of a chat's share link: the live link and the items it can tick. */
export interface ChatShareState {
  token: string | null;
  linkType: ShareLinkType | null;
  items: SharedItemOption[];
  candidates: SharedItemOption[];
}

interface RawShareState {
  token: string | null;
  link_type: ShareLinkType | null;
  items: SharedItemOption[];
  candidates: SharedItemOption[];
}

function toShareState(raw: RawShareState): ChatShareState {
  return { token: raw.token, linkType: raw.link_type, items: raw.items, candidates: raw.candidates };
}

/** Read the chat's share link and the private items it can grant; null on failure. */
export async function getChatShareState(conversationId: string): Promise<ChatShareState | null> {
  if (isHostedConversation(conversationId)) return null;
  try {
    const res = await fetchWithAuthRetry(
      `${apiBase()}/agent/conversations/${encodeURIComponent(conversationId)}/share`,
      { method: "GET" },
    );
    if (!res.ok) return null;
    return toShareState((await res.json()) as RawShareState);
  } catch {
    return null;
  }
}

/**
 * Create the chat's share link, or change its type and ticked items. The token
 * stays the same. Returns null on failure.
 */
export async function saveChatShare(
  conversationId: string,
  linkType: ShareLinkType,
  items: ReadonlyArray<Pick<SharedItemOption, "kind" | "id">>,
): Promise<ChatShareState | null> {
  if (isHostedConversation(conversationId)) return null;
  try {
    const res = await fetchWithAuthRetry(
      `${apiBase()}/agent/conversations/${encodeURIComponent(conversationId)}/share`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          link_type: linkType,
          items: linkType === "continue_data" ? items.map(({ kind, id }) => ({ kind, id })) : [],
        }),
      },
    );
    if (!res.ok) return null;
    return toShareState((await res.json()) as RawShareState);
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
  linkType: ShareLinkType;
  items: SharedItemOption[];
}

/**
 * List the user's chats that have a live share link, newest share first.
 * Throws on a failed request so the caller can show an error.
 */
export async function listSharedConversations(): Promise<SharedConversationSummary[]> {
  const res = await fetchWithAuthRetry(`${apiBase()}/agent/shared-conversations`, { method: "GET" });
  if (!res.ok) throw new Error(`shared chats list failed (${res.status})`);
  const raw = (await res.json()) as Array<{
    id: string;
    title: string;
    shared_at: string | null;
    token: string;
    link_type: ShareLinkType;
    items?: SharedItemOption[];
  }>;
  return raw.map((r) => ({
    id: r.id,
    title: r.title,
    sharedAt: r.shared_at,
    token: r.token,
    linkType: r.link_type,
    items: r.items ?? [],
  }));
}

/** Stop sharing a chat: its public link then answers "not found". Returns false on failure. */
export async function revokeConversationShare(conversationId: string): Promise<boolean> {
  if (isHostedConversation(conversationId)) return false;
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
  /** A continue link carries no messages: the reader must sign in first. */
  linkType: ShareLinkType;
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
    const raw = (await res.json()) as {
      title: string;
      messages: RawMessage[];
      link_type?: ShareLinkType;
    };
    return { title: raw.title, messages: raw.messages.map(toMessage), linkType: raw.link_type ?? "view" };
  } catch {
    return null;
  }
}

/** What a signed-in user opens from a share link. */
export interface SharedChatOpen {
  /** owner: the chat itself; fork: the caller's own copy; view: read-only. */
  mode: "owner" | "fork" | "view";
  conversationId: string | null;
}

/**
 * Open a share link as the signed-in user. For a continue link the first open
 * makes the user's own copy of the chat. Returns null for an unknown or
 * revoked link and on any failure.
 */
export async function openSharedConversation(token: string): Promise<SharedChatOpen | null> {
  try {
    const res = await fetchWithAuthRetry(
      `${apiBase()}/agent/shared-conversations/${encodeURIComponent(token)}/open`,
      { method: "POST" },
    );
    if (!res.ok) return null;
    const raw = (await res.json()) as { mode: SharedChatOpen["mode"]; conversation_id: string | null };
    return { mode: raw.mode, conversationId: raw.conversation_id };
  } catch {
    return null;
  }
}
