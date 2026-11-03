"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Plus, SidebarSimple } from "@/shared/ui/icons";
import { AgentThread } from "@/shared/ui/agent/agent-thread";
import { ChatErrorBanner } from "@/shared/ui/agent/chat-error-banner";
import { ChatTranscript } from "@/shared/ui/agent/chat-transcript";
import { Composer } from "@/shared/ui/agent/composer";
import { agentSteerChannel, newSteerKey, useSteerQueue } from "@/shared/ui/agent/use-steer-queue";
import { AppMark } from "@/shared/ui/app-mark";
import { EmptyState } from "@/shared/ui/empty-state";
import { Button } from "@/shared/ui/primitives/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import {
  MinimizedPill,
  ToolCallRow,
  dispatchGeneralistEvent,
  type GeneralistAgentHandlers,
} from "@/features/agent-panel";
import { fetchWithAuthRetry } from "@/shared/lib/api";
import { getRuntimeEnv } from "@/shared/lib/runtime-env";
import { readServerSentEvents } from "@/shared/lib/sse";
import {
  appendReasoning,
  closeThinking,
  lastEventAt,
  type ThinkingSegment,
} from "@/shared/lib/thinking-segments";
import { cn } from "@/shared/lib/utils";
import type { AgentToolCall } from "@/shared/ui/agent/types";
import type { SuggestFetcher } from "@/shared/ui/agent/use-next-prompt";
import { fillChrome, type DocsChrome } from "../lib/chrome";
import {
  MAX_KEPT_MESSAGES,
  MAX_QUESTION_CHARS,
  askBody,
  isDocsHref,
  isQuestionTooLong,
  parseStoredHistory,
  readSuggestion,
  toTranscript,
  trimHistory,
  type DocsAgentMessage,
} from "../lib/docs-answer";
import { discardStaleDocsResponse, docsAskFailure, type DocsAskNotice } from "../lib/docs-queue";

const STORAGE_KEY = "docs-agent-history";

/** A question handed over from the search box; the nonce makes a repeat count. */
export interface DocsAgentRequest {
  question: string;
  nonce: number;
}

/** End a turn: close its thinking, and mark a search that never returned as failed. */
function settle(message: DocsAgentMessage): DocsAgentMessage {
  if (message.role !== "assistant") return message;
  const now = Date.now();
  return {
    ...message,
    thinkingSegments: message.thinkingSegments && closeThinking(message.thinkingSegments, now),
    toolCalls: message.toolCalls?.map((call) =>
      call.status === "running" ? { ...call, status: "error", endedAt: now } : call,
    ),
  };
}

const renderToolCall = (call: AgentToolCall) => <ToolCallRow call={call} />;
const isStepCall = () => true;
const stepKind = () => "search" as const;

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
    | "agentTooLong"
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
  const [notice, setNotice] = React.useState<DocsAskNotice>(null);
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

  /** End the answer in flight; keep the text that already arrived. */
  const stopAnswer = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    const kept = trimHistory(messagesRef.current.map(settle), MAX_KEPT_MESSAGES);
    update(kept);
    saveHistory(kept);
  }, [update]);

  const askLatestRef = React.useRef<((question: string) => Promise<void>) | null>(null);
  const sendQueued = React.useCallback((text: string) => {
    if (!askLatestRef.current || isQuestionTooLong(text)) return false;
    void askLatestRef.current(text);
    return true;
  }, []);
  const { store: steerStore, midTurn, stop } = useSteerQueue({
    busy: streaming,
    send: sendQueued,
    stop: stopAnswer,
    maxLength: MAX_QUESTION_CHARS,
  });

  const ask = React.useCallback(
    async (question: string, earlier?: readonly DocsAgentMessage[]) => {
      const text = question.trim();
      if (!text) return;
      // Never send a cut question: keep the transcript as it is and tell the reader.
      if (isQuestionTooLong(text)) {
        setNotice("tooLong");
        return;
      }
      const before = earlier ?? messagesRef.current;
      abortRef.current?.abort();
      steerStore.setBusy(false);
      steerStore.setBusy(true);
      const controller = new AbortController();
      abortRef.current = controller;
      const steerKey = newSteerKey();
      const body = { ...askBody(text, before, locale), steer_key: steerKey };
      let base: DocsAgentMessage[] = [
        ...trimHistory(before, MAX_KEPT_MESSAGES),
        { role: "user", content: text, createdAt: Date.now() },
      ];
      let anchor = base[base.length - 1];
      let turn: DocsAgentMessage = { role: "assistant", content: "", toolCalls: [], thinkingSegments: [] };
      const show = (next: DocsAgentMessage) => {
        turn = next;
        update([...base, turn]);
      };
      const patchThinking = (
        change: (segments: readonly ThinkingSegment[], toolCount: number, since: number | null) => readonly ThinkingSegment[],
      ) => {
        const segments = turn.thinkingSegments ?? [];
        const calls = turn.toolCalls ?? [];
        show({ ...turn, thinkingSegments: change(segments, calls.length, lastEventAt(segments, calls)) });
      };
      show(turn);
      setNotice(null);
      setStreaming(true);
      let failed: DocsAskNotice = null;
      let done = false;
      // The same events, and the same thinking and tool rows, as the generalist agent.
      const handlers: GeneralistAgentHandlers = {
        onSteerApplied: ({ ids, text }) => {
          if (!steerStore.applied(ids)) return;
          if (!text.trim()) return;
          if (abortRef.current !== controller) {
            const current = messagesRef.current;
            const index = anchor ? current.indexOf(anchor) : -1;
            if (index >= 0) {
              const end = current[index + 1]?.role === "assistant" ? index + 2 : index + 1;
              anchor = { role: "user", content: text, createdAt: Date.now() };
              update([...current.slice(0, end), anchor, ...current.slice(end)]);
            }
            return;
          }
          base = [...base, settle(turn), { role: "user", content: text, createdAt: Date.now() }];
          anchor = base[base.length - 1];
          show({ role: "assistant", content: "", toolCalls: [], thinkingSegments: [] });
        },
        onReasoningPatch: (chunk) =>
          patchThinking((segments, toolCount, since) => appendReasoning(segments, chunk, Date.now(), toolCount, since)),
        onToolStart: (ev) => {
          patchThinking((segments) => closeThinking(segments, Date.now()));
          const call: AgentToolCall = {
            id: ev.id,
            tool: ev.tool,
            reason: ev.reason,
            status: "running",
            startedAt: Date.now(),
            endedAt: null,
            payload: { arguments: ev.arguments },
          };
          show({ ...turn, toolCalls: [...(turn.toolCalls ?? []), call] });
        },
        onToolEnd: (ev) =>
          show({
            ...turn,
            toolCalls: turn.toolCalls?.map((call) =>
              call.id === ev.id
                ? {
                    ...call,
                    status: ev.status === "ok" ? "done" : "error",
                    endedAt: Date.now(),
                    payload: { ...call.payload, result: ev.result },
                  }
                : call,
            ),
          }),
        onMessagePatch: (chunk) => {
          if (!turn.content) patchThinking((segments) => closeThinking(segments, Date.now()));
          show({ ...turn, content: turn.content + chunk });
        },
        onMessageReset: () => show({ ...turn, content: "" }),
        onDone: (result) => {
          done = true;
          show({ ...turn, content: result.assistant_message || turn.content, createdAt: Date.now() });
        },
        onError: () => {
          failed = "error";
        },
      };
      try {
        const res = await fetchWithAuthRetry(`${getRuntimeEnv().apiUrl}/docs-agent/ask`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        // Stop or the replacement ask owns the streaming state.
        if (await discardStaleDocsResponse(
          res,
          controller.signal.aborted || abortRef.current !== controller,
        )) return;
        failed = docsAskFailure(res.status, Boolean(res.body), () => steerStore.pause());
        if (!failed && res.body) {
          await readServerSentEvents(res.body, (event) => {
            if (controller.signal.aborted || abortRef.current !== controller) return;
            if (event.event === "steer_ready" && event.data.steer_key === steerKey) {
              steerStore.openTurn(agentSteerChannel(steerKey, (event) => {
                handlers.onSteerApplied?.(event);
                saveHistory(messagesRef.current);
              }));
              return;
            }
            dispatchGeneralistEvent(handlers, event.event, event.data, () => labels.agentError);
          });
          if (!failed && (!done || !turn.content.trim())) failed = "error";
        }
      } catch {
        if (controller.signal.aborted) return;
        failed = "error";
      }
      if (abortRef.current !== controller) return;
      // Steers the server reports late are placed after their anchor, not after this answer.
      abortRef.current = null;
      if (failed && failed !== "tooLong") steerStore.pause();
      setStreaming(false);
      // A question refused while this answer streamed keeps its "too long" notice.
      setNotice((prev) => failed ?? (prev === "tooLong" ? prev : null));
      const final = trimHistory([...base, settle(turn)], MAX_KEPT_MESSAGES);
      update(final);
      saveHistory(final);
    },
    [labels.agentError, locale, update, steerStore],
  );

  React.useEffect(() => {
    askLatestRef.current = ask;
  }, [ask]);

  // Run each search-box question once; a locale change must not send it again.
  const handledRequestRef = React.useRef<DocsAgentRequest | null>(null);
  React.useEffect(() => {
    if (!request || handledRequestRef.current === request) return;
    handledRequestRef.current = request;
    // ask() refuses a too-long question; put it in the composer so the reader can shorten it.
    if (isQuestionTooLong(request.question.trim())) setDraft(request.question);
    void ask(request.question);
  }, [ask, request]);

  const clear = () => {
    steerStore.clear();
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
    setNotice(null);
    update([]);
    saveHistory([]);
  };

  const submit = () => {
    if (streaming || !draft.trim()) return;
    // Keep the draft so the reader can shorten it.
    if (isQuestionTooLong(draft)) {
      setNotice("tooLong");
      return;
    }
    void ask(draft);
    setDraft("");
  };

  const editAndResend = (index: number, content: string) => {
    if (streaming) return false;
    if (isQuestionTooLong(content)) {
      setNotice("tooLong");
      return false;
    }
    void ask(content, messagesRef.current.slice(0, index));
  };

  const transcript = React.useMemo(() => toTranscript(messages), [messages]);
  // The docs route keeps the signed-out access, limits and budget of the ask
  // route; the generalist's endpoint needs sign-in and bills the user.
  const fetchSuggestion = React.useCallback<SuggestFetcher>(
    async ({ turns, actions }) => {
      const res = await fetchWithAuthRetry(`${getRuntimeEnv().apiUrl}/docs-agent/suggest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ turns, actions, locale }),
      });
      return res.ok ? readSuggestion(await res.json()) : "";
    },
    [locale],
  );
  const suggest = React.useMemo(
    () => ({ messages: transcript, fetcher: fetchSuggestion }),
    [transcript, fetchSuggestion],
  );

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
                scrollDeps={[
                  messages.length,
                  messages[messages.length - 1]?.content,
                  messages[messages.length - 1]?.toolCalls?.length,
                  messages[messages.length - 1]?.thinkingSegments?.at(-1)?.reasoning,
                  notice,
                ]}
              >
                <ChatTranscript
                  messages={transcript}
                  streaming={streaming}
                  editAndResend={editAndResend}
                  allowLink={isDocsHref}
                  renderToolCall={renderToolCall}
                  isStepCall={isStepCall}
                  stepKind={stepKind}
                  animatePairs
                  trailing={() =>
                    notice && (
                      <ChatErrorBanner
                        message={
                          notice === "rate"
                            ? labels.agentRateLimited
                            : notice === "tooLong"
                              ? fillChrome(labels.agentTooLong, { limit: MAX_QUESTION_CHARS.toLocaleString(locale) })
                              : labels.agentError
                        }
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
                midTurn={midTurn}
                acceptsMidTurn={(text) => {
                  if (!isQuestionTooLong(text)) return true;
                  setNotice("tooLong");
                  return false;
                }}
                sendAriaLabel={labels.agentSend}
                attachments={false}
                dictation={false}
                suggest={suggest}
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
