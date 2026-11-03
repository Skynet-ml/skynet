"use client";

import * as React from "react";
import { toast } from "react-toastify";
import { formatMsg, msg } from "@/shared/lib/messages";
import { formatBudgetUsd } from "@/features/billing";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";

import type { AgentMessage, AgentStatus, AgentToolCall } from "@/shared/ui/agent/types";
import type { ChatTurn, PendingApprovalPayload, TrustMode } from "@/features/agent-panel";
import type { MidTurnQueue } from "@/shared/ui/agent/steer-queue";
import { agentSteerChannel, newSteerKey, useSteerQueue } from "@/shared/ui/agent/use-steer-queue";

import { confirmReactServeApproval, streamReactServeChat } from "../lib/react-chat-stream";
import {
  appendReactChatSteer,
  reactChatCanSteer,
  REACT_CHAT_USER_MESSAGE_MAX_CHARS,
  removeReplyBeforeSteer,
} from "../lib/react-chat-steering";

export interface ReactServeChatState {
  midTurn: MidTurnQueue;
  status: AgentStatus;
  statusLabel: string;
  messages: AgentMessage[];
  reasoning: string;
  reasoningStartedAt: number | null;
  reasoningEndedAt: number | null;
  error: string | null;
  pendingApproval: PendingApprovalPayload | null;
  send: (message: string) => boolean;
  editAndResend: (messageIndex: number, content: string) => void;
  retry: () => void;
  stop: () => void;
  confirmApproval: (approved: boolean) => Promise<void>;
}

// Trimmed sibling of useGeneralistAgent for the served-ReAct chat playground.
// Same SSE event handling and message/tool-call assembly so the shared agent
// primitives render identically, minus the wizard-state, conversation
// persistence, and dataset/code-authoring concerns the generalist panel owns.
export function useReactServeChat(
  optimizationId: string,
  trustMode: TrustMode,
): ReactServeChatState {
  const [status, setStatus] = React.useState<AgentStatus>("idle");
  const [statusLabel, setStatusLabel] = React.useState("");
  const [messages, setMessages] = React.useState<AgentMessage[]>([]);
  const [reasoning, setReasoning] = React.useState("");
  const [reasoningStartedAt, setReasoningStartedAt] = React.useState<number | null>(null);
  const [reasoningEndedAt, setReasoningEndedAt] = React.useState<number | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [pendingApproval, setPendingApproval] = React.useState<PendingApprovalPayload | null>(null);

  const abortRef = React.useRef<AbortController | null>(null);
  const sendLatestRef = React.useRef<(text: string) => boolean>(() => false);
  const sendQueued = React.useCallback((text: string) => sendLatestRef.current(text), []);
  const stopTurn = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStatus("idle");
    setStatusLabel("");
    setPendingApproval(null);
  }, []);
  const { store: steerStore, midTurn, stop } = useSteerQueue({
    busy: status === "streaming",
    send: sendQueued,
    stop: stopTurn,
    maxLength: REACT_CHAT_USER_MESSAGE_MAX_CHARS,
  });
  const reasoningBufRef = React.useRef("");
  const replyBufRef = React.useRef("");
  const replyBeforeSteerRef = React.useRef<string | null>(null);
  const messagesRef = React.useRef<AgentMessage[]>(messages);
  React.useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  const trustRef = React.useRef(trustMode);
  React.useEffect(() => {
    trustRef.current = trustMode;
  }, [trustMode]);

  React.useEffect(
    () => () => {
      abortRef.current?.abort();
      abortRef.current = null;
    },
    [],
  );

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
      const next = prev.slice();
      next[next.length - 1] = { ...last, toolCalls: [...(last.toolCalls ?? []), call] };
      return next;
    });
  }, []);

  const finishToolCall = React.useCallback(
    (id: string, nextStatus: "done" | "error", result?: unknown) => {
      setMessages((prev) => {
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
    [],
  );

  const runAgent = React.useCallback(
    (userMessage: string, history: AgentMessage[]): boolean => {
      abortRef.current?.abort();
      steerStore.setBusy(false);
      steerStore.setBusy(true);
      const steerKey = newSteerKey();
      const controller = new AbortController();
      abortRef.current = controller;

      reasoningBufRef.current = "";
      replyBufRef.current = "";
      replyBeforeSteerRef.current = null;

      setStatus("streaming");
      setStatusLabel(msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.1"));
      setReasoning("");
      setReasoningStartedAt(Date.now());
      setReasoningEndedAt(null);
      setError(null);
      setPendingApproval(null);

      setMessages((m) => [
        ...m,
        { role: "user", content: userMessage, createdAt: Date.now() },
        { role: "assistant", content: "", toolCalls: [] },
      ]);

      const chatHistory: ChatTurn[] = history
        .filter((m) => m.content.trim().length > 0)
        .map((m) => ({ role: m.role, content: m.content }));

      void streamReactServeChat(
        optimizationId,
        {
          steer_key: steerKey,
          user_message: userMessage,
          chat_history: chatHistory,
          trust_mode: trustRef.current,
        },
        crypto.randomUUID(),
        {
          signal: controller.signal,
          onSteerReady: (key) => {
            if (controller.signal.aborted || !reactChatCanSteer(key, steerKey)) return;
            steerStore.openTurn(agentSteerChannel(steerKey));
          },
          onSteerApplied: ({ ids, text }) => {
            if (controller.signal.aborted) return;
            steerStore.applied(ids);
            if (!text.trim()) return;
            replyBeforeSteerRef.current = (replyBeforeSteerRef.current ?? "") + replyBufRef.current;
            replyBufRef.current = "";
            setMessages((prev) => appendReactChatSteer(prev, text));
          },
          onReasoningPatch: (chunk) => {
            if (controller.signal.aborted) return;
            if (reasoningBufRef.current === "") setReasoningStartedAt(Date.now());
            reasoningBufRef.current += chunk;
            setReasoning(reasoningBufRef.current);
          },
          onToolStart: (ev) => {
            if (controller.signal.aborted) return;
            setStatusLabel(
              formatMsg("auto.features.agent.panel.hooks.use.generalist.agent.template.1", {
                p1: ev.tool,
              }),
            );
            pushToolCall({
              id: ev.id,
              tool: ev.tool,
              reason: ev.reason,
              status: "running",
              startedAt: Date.now(),
              endedAt: null,
              payload: { arguments: ev.arguments },
            });
          },
          onToolEnd: (ev) => {
            if (controller.signal.aborted) return;
            finishToolCall(ev.id, ev.status === "ok" ? "done" : "error", ev.result);
          },
          onPendingApproval: (ev) => {
            if (controller.signal.aborted) return;
            setPendingApproval(ev);
            setStatusLabel(msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.2"));
          },
          onApprovalResolved: () => {
            if (controller.signal.aborted) return;
            setPendingApproval(null);
            setStatusLabel(msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.3"));
          },
          onMessagePatch: (chunk) => {
            if (controller.signal.aborted) return;
            if (replyBufRef.current === "") {
              setStatusLabel(msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.4"));
              if (reasoningBufRef.current) setReasoningEndedAt(Date.now());
            }
            replyBufRef.current += chunk;
            appendReply(chunk);
          },
          onDone: (result) => {
            if (controller.signal.aborted) return;
            setStatus("done");
            setStatusLabel("");
            if (result.cents_charged != null) {
              toast.success(
                formatMsg("optimizations.serve.request_spent", {
                  cents: formatBudgetUsd(String(result.cents_charged), getActiveIntlLocale()),
                }),
              );
            }
            if (reasoningBufRef.current) setReasoningEndedAt(Date.now());
            setMessages((prev) => {
              const last = prev[prev.length - 1];
              if (!last || last.role !== "assistant") return prev;
              let reply = result.assistant_message;
              if (replyBeforeSteerRef.current !== null) {
                reply = last.content || removeReplyBeforeSteer(reply, replyBeforeSteerRef.current);
              }
              const fallback =
                last.content ||
                (last.toolCalls?.length
                  ? ""
                  : msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.5"));
              const next = prev.slice();
              next[next.length - 1] = {
                ...last,
                content: reply || fallback,
                model: result.model,
                createdAt: Date.now(),
              };
              return next;
            });
          },
          onError: (message) => {
            if (controller.signal.aborted) return;
            setStatus("error");
            setStatusLabel(msg("auto.features.agent.panel.hooks.use.generalist.agent.literal.6"));
            setError(message);
            setMessages((prev) => {
              const last = prev[prev.length - 1];
              if (!last || last.role !== "assistant") return prev;
              if (!last.content && !last.toolCalls?.length) return prev.slice(0, -1);
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
          },
        },
      );
      return true;
    },
    [optimizationId, appendReply, pushToolCall, finishToolCall, steerStore],
  );

  const send = React.useCallback(
    (message: string) => {
      const trimmed = message.trim();
      if (!trimmed || trimmed.length > REACT_CHAT_USER_MESSAGE_MAX_CHARS) return false;
      return runAgent(trimmed, messagesRef.current);
    },
    [runAgent],
  );
  React.useEffect(() => {
    sendLatestRef.current = send;
  }, [send]);

  const editAndResend = React.useCallback(
    (messageIndex: number, content: string) => {
      const trimmed = content.trim();
      if (!trimmed || trimmed.length > REACT_CHAT_USER_MESSAGE_MAX_CHARS) return;
      steerStore.pause();
      abortRef.current?.abort();
      abortRef.current = null;
      const truncated = messagesRef.current.slice(0, messageIndex);
      setMessages(truncated);
      messagesRef.current = truncated;
      runAgent(trimmed, truncated);
    },
    [runAgent, steerStore],
  );

  const retry = React.useCallback(() => {
    const current = messagesRef.current;
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
    steerStore.pause();
    abortRef.current?.abort();
    abortRef.current = null;
    const truncated = current.slice(0, lastUserIndex);
    setMessages(truncated);
    messagesRef.current = truncated;
    runAgent(lastUser.content, truncated);
  }, [runAgent, steerStore]);

  const confirmApproval = React.useCallback(
    async (approved: boolean) => {
      const pa = pendingApproval;
      if (!pa) return;
      setPendingApproval(null);
      await confirmReactServeApproval(optimizationId, pa.id, approved);
    },
    [pendingApproval, optimizationId],
  );

  return {
    midTurn,
    status,
    statusLabel,
    messages,
    reasoning,
    reasoningStartedAt,
    reasoningEndedAt,
    error,
    pendingApproval,
    send,
    editAndResend,
    retry,
    stop,
    confirmApproval,
  };
}
