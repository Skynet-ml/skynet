"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Plus, SidebarSimple } from "@/shared/ui/icons";
import { AgentThread } from "@/shared/ui/agent/agent-thread";
import { ChatErrorBanner } from "@/shared/ui/agent/chat-error-banner";
import { ChatTranscript } from "@/shared/ui/agent/chat-transcript";
import { Composer } from "@/shared/ui/agent/composer";
import { AppMark } from "@/shared/ui/app-mark";
import { EmptyState } from "@/shared/ui/empty-state";
import { Button } from "@/shared/ui/primitives/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { MinimizedPill } from "@/features/agent-panel";
import { fetchWithAuthRetry } from "@/shared/lib/api";
import { getRuntimeEnv } from "@/shared/lib/runtime-env";
import { readServerSentEvents } from "@/shared/lib/sse";
import { cn } from "@/shared/lib/utils";
import type { DocsChrome } from "../lib/chrome";
import {
  MAX_KEPT_MESSAGES,
  askBody,
  chunkText,
  isDocsHref,
  parseStoredHistory,
  toTranscript,
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
 * The docs agent: the app's agent side panel and agent pill, built from the
 * same shared chat parts as the generalist panel, answering from the docs.
 *
 * It portals to <body>: the docs header's backdrop blur makes the header the
 * containing block of any `position: fixed` child, which pushed the old card
 * off the top of the screen.
 */
export function DocsAgentPanel({
  locale,
  dir,
  labels,
  request,
  open,
  onOpen,
  onClose,
}: {
  locale: string;
  dir: "ltr" | "rtl";
  labels: Pick<
    DocsChrome,
    | "askAgentHint"
    | "agentTitle"
    | "agentPlaceholder"
    | "agentRateLimited"
    | "agentError"
    | "agentClear"
    | "agentClose"
    | "agentSend"
  >;
  request: DocsAgentRequest | null;
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
}) {
  const [messages, setMessages] = React.useState<DocsAgentMessage[]>([]);
  const [streaming, setStreaming] = React.useState(false);
  const [notice, setNotice] = React.useState<Notice>(null);
  const [draft, setDraft] = React.useState("");
  const [mounted, setMounted] = React.useState(false);
  const messagesRef = React.useRef<DocsAgentMessage[]>([]);
  const abortRef = React.useRef<AbortController | null>(null);
  const reduceMotion = useReducedMotion();
  const isRtl = dir === "rtl";

  const update = React.useCallback((next: DocsAgentMessage[]) => {
    messagesRef.current = next;
    setMessages(next);
  }, []);

  // sessionStorage and document.body exist only in the browser, so both wait for mount.
  React.useEffect(() => {
    setMounted(true);
    update(loadHistory());
    return () => abortRef.current?.abort();
  }, [update]);

  const ask = React.useCallback(
    async (question: string, earlier?: readonly DocsAgentMessage[]) => {
      const text = question.trim();
      if (!text) return;
      const before = earlier ?? messagesRef.current;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const body = askBody(text, before, locale);
      let answer = "";
      const base = [...trimHistory(before, MAX_KEPT_MESSAGES), { role: "user" as const, content: text }];
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

  /** End the answer in flight; keep the text that already arrived. */
  const stop = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    const kept = trimHistory(messagesRef.current, MAX_KEPT_MESSAGES);
    update(kept);
    saveHistory(kept);
  };

  const clear = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    setNotice(null);
    update([]);
    saveHistory([]);
  };

  const submit = () => {
    if (streaming || !draft.trim()) return;
    void ask(draft);
    setDraft("");
  };

  const editAndResend = (index: number, content: string) => {
    if (streaming) return false;
    void ask(content, messagesRef.current.slice(0, index));
  };

  const transcript = React.useMemo(() => toTranscript(messages), [messages]);

  if (!mounted) return null;

  return createPortal(
    <>
      <AnimatePresence>
        {open && (
          <motion.div
            key="docs-agent-backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={onClose}
            aria-hidden="true"
            className="fixed inset-0 z-40 bg-black/50 lg:hidden"
          />
        )}
        {open && (
          <motion.aside
            key="docs-agent-panel"
            dir={dir}
            aria-label={labels.agentTitle}
            initial={reduceMotion ? false : { x: isRtl ? -24 : 24, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={reduceMotion ? { opacity: 0 } : { x: isRtl ? -24 : 24, opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.2, 0.8, 0.2, 1] }}
            onKeyDown={(event) => {
              if (event.key === "Escape" && !event.defaultPrevented) onClose();
            }}
            className={cn(
              "fixed end-0 inset-y-0 z-40 flex h-dvh w-screen shrink-0 md:w-[min(480px,92vw)]",
              "border-s border-border/60 bg-background/95 backdrop-blur-xl",
              isRtl ? "shadow-[8px_0_32px_rgba(0,0,0,0.06)]" : "shadow-[-8px_0_32px_rgba(0,0,0,0.06)]",
            )}
          >
            <div className="relative flex min-w-0 flex-1 flex-col">
              <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border/40 px-3 py-2.5">
                <div className="flex min-w-0 items-center gap-2">
                  <span className="inline-flex size-6 shrink-0 overflow-hidden rounded-full bg-secondary">
                    <AppMark className="size-full" aria-hidden="true" />
                  </span>
                  <span className="truncate text-[0.8125rem] font-medium leading-tight">{labels.agentTitle}</span>
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  {messages.length > 0 && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-xs"
                          onClick={clear}
                          className="text-muted-foreground hover:text-foreground"
                          aria-label={labels.agentClear}
                        >
                          <Plus className="size-3.5" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent side="bottom">{labels.agentClear}</TooltipContent>
                    </Tooltip>
                  )}
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        onClick={onClose}
                        className="text-muted-foreground hover:text-foreground"
                        aria-label={labels.agentClose}
                      >
                        <SidebarSimple className="size-3.5" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent side="bottom">{labels.agentClose}</TooltipContent>
                  </Tooltip>
                </div>
              </div>

              <AgentThread
                className="overscroll-contain"
                isEmpty={messages.length === 0}
                emptyState={<EmptyState icon={AppMark} variant="compact" title={labels.askAgentHint} />}
                scrollDeps={[messages.length, messages[messages.length - 1]?.content, notice]}
              >
                <ChatTranscript
                  messages={transcript}
                  streaming={streaming}
                  editAndResend={editAndResend}
                  allowLink={isDocsHref}
                  animatePairs
                  trailing={() =>
                    notice && (
                      <ChatErrorBanner
                        message={notice === "rate" ? labels.agentRateLimited : labels.agentError}
                      />
                    )
                  }
                />
              </AgentThread>

              <Composer
                value={draft}
                onChange={setDraft}
                onSubmit={submit}
                onStop={stop}
                placeholder={labels.agentPlaceholder}
                streaming={streaming}
                sendAriaLabel={labels.agentSend}
              />
            </div>
          </motion.aside>
        )}
      </AnimatePresence>

      {!open && <MinimizedPill onOpen={onOpen} active={streaming} label={labels.agentTitle} dir={dir} />}
    </>,
    document.body,
  );
}
