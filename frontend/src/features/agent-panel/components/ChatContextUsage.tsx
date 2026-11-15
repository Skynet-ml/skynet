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
  streaming = false,
}: {
  messages: readonly AgentMessage[];
  contextWindow: number | null;
  streaming?: boolean;
}) {
  const window = contextWindow && contextWindow > 0 ? contextWindow : DEFAULT_CONTEXT_WINDOW;
  // Each streamed token makes a new `messages`, and the count stringifies every
  // tool result. Hold the count of the last settled list until the turn ends.
  const [settled, setSettled] = React.useState(messages);
  if (!streaming && settled !== messages) setSettled(messages);
  const used = React.useMemo(() => contextTokens(settled, window), [settled, window]);
  return <ContextMeter used={used} window={window} />;
}
