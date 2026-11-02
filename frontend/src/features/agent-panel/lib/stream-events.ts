import type { GeneralistAgentHandlers } from "./stream";

/**
 * Route one generalist-agent SSE event to its handler.
 *
 * Kept free of runtime imports so the unit-test runner can load it.
 */
export function dispatchGeneralistEvent(
  handlers: GeneralistAgentHandlers,
  event: string,
  data: Record<string, unknown>,
  unknownError: () => string,
): void {
  switch (event) {
    case "reasoning_patch":
      handlers.onReasoningPatch?.(String(data.chunk ?? ""));
      break;
    case "thinking_summary": {
      const text = String(data.text ?? "").trim();
      if (typeof data.index === "number" && text)
        handlers.onThinkingSummary?.({ index: data.index, text });
      break;
    }
    case "plan": {
      const text = String(data.text ?? "").trim();
      if (text) handlers.onPlan?.(text);
      break;
    }
    case "tool_pending": {
      const tool = String(data.tool ?? "");
      if (tool) handlers.onToolPending?.({ tool });
      break;
    }
    case "tool_pending_clear":
      handlers.onToolPendingClear?.();
      break;
    case "tool_start":
      handlers.onToolStart?.({
        id: String(data.id ?? ""),
        tool: String(data.tool ?? ""),
        reason: String(data.reason ?? ""),
        arguments: (data.arguments as Record<string, unknown>) ?? {},
      });
      break;
    case "tool_end":
      handlers.onToolEnd?.({
        id: String(data.id ?? ""),
        tool: String(data.tool ?? ""),
        status: String(data.status ?? "ok"),
        result: data.result,
      });
      break;
    // The status text is written in the server's language, not the viewer's;
    // the panel words its own status from tool_start and tool_end instead.
    case "status_patch":
      break;
    case "pending_approval":
      handlers.onPendingApproval?.({
        id: String(data.id ?? ""),
        tool: String(data.tool ?? ""),
        arguments: (data.arguments as Record<string, unknown>) ?? {},
      });
      break;
    case "approval_resolved":
      handlers.onApprovalResolved?.({
        id: String(data.id ?? ""),
        tool: String(data.tool ?? ""),
        approved: Boolean(data.approved),
      });
      break;
    case "message_patch":
      handlers.onMessagePatch?.(String(data.chunk ?? ""));
      break;
    case "message_reset":
      handlers.onMessageReset?.();
      break;
    case "steer_applied":
      handlers.onSteerApplied?.({
        ids: Array.isArray(data.ids) ? data.ids.map(String) : [],
        text: String(data.text ?? ""),
      });
      break;
    case "conversation_meta":
      handlers.onConversationMeta?.({
        conversation_id: String(data.conversation_id ?? ""),
        title: String(data.title ?? ""),
      });
      break;
    case "done": {
      const rawModel = data.model;
      const rawServed = data.served_model;
      handlers.onDone({
        assistant_message: String(data.assistant_message ?? ""),
        model: typeof rawModel === "string" && rawModel.length > 0 ? rawModel : null,
        served_model: typeof rawServed === "string" && rawServed.length > 0 ? rawServed : null,
        stats: data.stats ?? null,
      });
      break;
    }
    case "turn_stats":
      if (data.stats) handlers.onTurnStats?.(data.stats);
      break;
    case "error":
      handlers.onError(
        String(data.error ?? unknownError()),
        typeof data.code === "string" ? data.code : undefined,
      );
      break;
  }
}
