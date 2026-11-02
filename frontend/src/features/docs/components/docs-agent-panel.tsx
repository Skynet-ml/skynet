"use client";

import * as React from "react";
import { ArrowCounterClockwise, PaperPlaneTilt, Sparkle, X } from "@/shared/ui/icons";
import { MessageMarkdown } from "@/shared/ui/agent/message-markdown";
import { fetchWithAuthRetry } from "@/shared/lib/api";
import { getRuntimeEnv } from "@/shared/lib/runtime-env";
import { readServerSentEvents } from "@/shared/lib/sse";
import { cn } from "@/shared/lib/utils";
import type { DocsChrome } from "../lib/chrome";
import {
  MAX_KEPT_MESSAGES,
  askBody,
  chunkText,
  keepDocsLinks,
  parseStoredHistory,
  trimHistory,
  type DocsAgentMessage,
} from "../lib/docs-answer";

const STORAGE_KEY = "docs-agent-history";

type Notice = "rate" | "error" | null;

/** A question handed over from the search box; the nonce makes a repeat count. */
export interface DocsAgentRequest {
  question: string;
  nonce: number;
}

/** Load the session history; storage can be blocked, and then the panel starts empty. */
function loadHistory(): DocsAgentMessage[] {
  try {
    return parseStoredHistory(window.sessionStorage.getItem(STORAGE_KEY));
  } catch {
    return [];
  }
}

/** Save the session history, ignoring blocked storage. */
function saveHistory(messages: DocsAgentMessage[]): void {
  try {
    window.sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify(trimHistory(messages, MAX_KEPT_MESSAGES)),
    );
  } catch {
    /* storage unavailable */
  }
}

/**
 * The docs agent answer panel: a small non-modal card at the bottom corner
 * that streams answers from the docs and keeps a short session history.
 */
export function DocsAgentPanel({
  locale,
  dir,
  labels,
  request,
  open,
  onClose,
}: {
  locale: string;
  dir: "ltr" | "rtl";
  labels: Pick<
    DocsChrome,
    | "agentTitle"
    | "agentPlaceholder"
    | "agentThinking"
    | "agentRateLimited"
    | "agentError"
    | "agentClear"
    | "agentClose"
    | "agentSend"
  >;
  request: DocsAgentRequest | null;
  open: boolean;
  onClose: () => void;
}) {
  const [messages, setMessages] = React.useState<DocsAgentMessage[]>([]);
  const [streaming, setStreaming] = React.useState(false);
  const [notice, setNotice] = React.useState<Notice>(null);
  const [draft, setDraft] = React.useState("");
  const messagesRef = React.useRef<DocsAgentMessage[]>([]);
  const abortRef = React.useRef<AbortController | null>(null);
  const logRef = React.useRef<HTMLDivElement | null>(null);
  const inputRef = React.useRef<HTMLInputElement | null>(null);

  const update = React.useCallback((next: DocsAgentMessage[]) => {
    messagesRef.current = next;
    setMessages(next);
  }, []);

  // sessionStorage exists only in the browser, so history loads after mount.
  React.useEffect(() => {
    update(loadHistory());
    return () => abortRef.current?.abort();
  }, [update]);

  const ask = React.useCallback(
    async (question: string) => {
      const text = question.trim();
      if (!text) return;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const body = askBody(text, messagesRef.current, locale);
      let answer = "";
      const base = [...trimHistory(messagesRef.current, MAX_KEPT_MESSAGES), { role: "user" as const, content: text }];
      update([...base, { role: "assistant", content: "" }]);
      setNotice(null);
      setStreaming(true);
      let failed: Notice = null;
      try {
        const res = await fetchWithAuthRetry(`${getRuntimeEnv().apiUrl}/docs-agent/ask`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        if (res.status === 429) failed = "rate";
        else if (!res.ok || !res.body) failed = "error";
        else {
          await readServerSentEvents(res.body, (event) => {
            if (event.event === "error" || "error" in event.data) {
              failed = "error";
              return;
            }
            const piece = chunkText(event.data);
            if (!piece) return;
            answer += piece;
            update([...base, { role: "assistant", content: answer }]);
          });
          if (!answer && !failed) failed = "error";
        }
      } catch {
        if (controller.signal.aborted) return;
        failed = "error";
      }
      if (abortRef.current !== controller) return;
      setStreaming(false);
      setNotice(failed);
      const final = trimHistory([...base, { role: "assistant", content: answer }], MAX_KEPT_MESSAGES);
      update(final);
      saveHistory(final);
    },
    [locale, update],
  );

  React.useEffect(() => {
    if (request) void ask(request.question);
  }, [ask, request]);

  React.useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [messages, notice]);

  const clear = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    setNotice(null);
    update([]);
    saveHistory([]);
    inputRef.current?.focus();
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (streaming || !draft.trim()) return;
    void ask(draft);
    setDraft("");
  };

  if (!open) return null;

  const last = messages[messages.length - 1];
  const thinking = streaming && last?.role === "assistant" && !last.content;

  return (
    <section
      role="dialog"
      aria-modal="false"
      aria-labelledby="docs-agent-title"
      dir={dir}
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}
      className="fixed bottom-4 end-4 z-50 flex max-h-[min(36rem,calc(100dvh-6rem))] w-[min(28rem,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-border/75 bg-background shadow-[var(--shadow-float)] motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-2 motion-safe:duration-200 motion-safe:ease-out"
    >
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border/60 ps-4 pe-2">
        <Sparkle className="size-4 shrink-0 text-primary" aria-hidden="true" />
        <h2 id="docs-agent-title" className="min-w-0 flex-1 truncate text-sm font-semibold">
          {labels.agentTitle}
        </h2>
        <button
          type="button"
          onClick={clear}
          disabled={messages.length === 0 && !streaming}
          aria-label={labels.agentClear}
          title={labels.agentClear}
          className="inline-flex size-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent/55 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/35 disabled:pointer-events-none disabled:opacity-40"
        >
          <ArrowCounterClockwise className="size-4" aria-hidden="true" />
        </button>
        <button
          type="button"
          onClick={onClose}
          aria-label={labels.agentClose}
          title={labels.agentClose}
          className="inline-flex size-8 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent/55 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/35"
        >
          <X className="size-4" aria-hidden="true" />
        </button>
      </header>

      <div
        ref={logRef}
        role="log"
        aria-busy={streaming}
        className="min-h-24 flex-1 space-y-4 overflow-y-auto px-4 py-4 text-sm"
      >
        {messages.map((message, index) =>
          message.role === "user" ? (
            <p
              key={index}
              dir="auto"
              className="ms-auto w-fit max-w-[85%] whitespace-pre-wrap rounded-2xl bg-muted px-3 py-2 text-start text-foreground"
            >
              {message.content}
            </p>
          ) : message.content ? (
            <div key={index} dir="auto" className="leading-relaxed text-foreground/90">
              <MessageMarkdown content={keepDocsLinks(message.content)} />
            </div>
          ) : null,
        )}
        {thinking && (
          <p className="text-muted-foreground motion-safe:animate-pulse">{labels.agentThinking}</p>
        )}
        <div aria-live="polite">
          {notice && (
            <p
              className={cn(
                "rounded-xl px-3 py-2 text-start",
                notice === "rate"
                  ? "bg-muted text-foreground"
                  : "bg-destructive/10 text-destructive",
              )}
            >
              {notice === "rate" ? labels.agentRateLimited : labels.agentError}
            </p>
          )}
        </div>
      </div>

      <form onSubmit={submit} className="flex shrink-0 items-center gap-2 border-t border-border/60 p-2">
        <input
          ref={inputRef}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={labels.agentPlaceholder}
          aria-label={labels.agentPlaceholder}
          autoComplete="off"
          dir="auto"
          className="h-9 min-w-0 flex-1 rounded-lg bg-transparent px-2 text-start text-sm text-foreground outline-none placeholder:text-muted-foreground/90 focus-visible:ring-2 focus-visible:ring-primary/35"
        />
        <button
          type="submit"
          disabled={streaming || !draft.trim()}
          aria-label={labels.agentSend}
          title={labels.agentSend}
          className="inline-flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-opacity hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/35 disabled:pointer-events-none disabled:opacity-40"
        >
          <PaperPlaneTilt className="size-4 rtl:-scale-x-100" aria-hidden="true" />
        </button>
      </form>
    </section>
  );
}
