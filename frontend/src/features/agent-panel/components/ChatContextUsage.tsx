"use client";

import * as React from "react";

import { ContextMeter } from "@/shared/ui/agent/context-meter";
import type { AgentMessage } from "@/shared/ui/agent/types";

import { DEFAULT_CONTEXT_WINDOW, contextTokens } from "../lib/session-stats";

/**
 * The composer's context usage: about how many tokens the next turn sends,
 * against the model's window. The window is the catalog's `max_input_tokens`
 * for the chat's model, or the backend's default without it.
 */
export function ChatContextUsage({
  messages,
  contextWindow,
}: {
  messages: readonly AgentMessage[];
  contextWindow: number | null;
}) {
  const window = contextWindow && contextWindow > 0 ? contextWindow : DEFAULT_CONTEXT_WINDOW;
  const used = React.useMemo(() => contextTokens(messages, window), [messages, window]);
  return <ContextMeter used={used} window={window} />;
}
