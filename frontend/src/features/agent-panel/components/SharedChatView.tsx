"use client";

import * as React from "react";

import { AgentBubble } from "@/shared/ui/agent/agent-bubble";
import { UserBubble } from "@/shared/ui/agent/user-bubble";
import type { AgentMessage, AgentToolCall } from "@/shared/ui/agent/types";

import { hideKickoffs } from "../lib/screen-agent";
import { ToolCallRow } from "./ToolCallRow";
import { getToolKind, getToolRenderer } from "./tool-renderers";

// The request tools open cards that upload data or run inference. A viewer
// of a shared chat must not act on them, so they show as plain rows.
const REQUEST_TOOLS: ReadonlySet<string> = new Set([
  "request_user_dataset_datasets_request_upload_post",
  "request_user_dataset_from_library",
  "request_user_inference",
  "request_user_pair_inference",
]);
const SUBMIT_TOOLS: ReadonlySet<string> = new Set(["submit_unified_run_optimize_post"]);

function renderToolCall(call: AgentToolCall, ctx: { isRetry: boolean }): React.ReactNode {
  const renderer = REQUEST_TOOLS.has(call.tool) ? undefined : getToolRenderer(call.tool);
  if (renderer?.card && call.status !== "running") return renderer.card(call);
  const summary = renderer?.summary?.(call) ?? null;
  return <ToolCallRow call={call} isRetry={ctx.isRetry} summary={summary} />;
}

function isStepCall(call: AgentToolCall): boolean {
  return !(SUBMIT_TOOLS.has(call.tool) && call.status !== "running");
}

function stepKind(call: AgentToolCall) {
  return getToolKind(call.tool);
}

/**
 * A read-only replica of an agent chat for a share link: the same bubbles
 * and tool cards as the live panel, with no composer, no approvals, no
 * edit-and-resend and no regenerate.
 */
export function SharedChatView({ messages }: { messages: readonly AgentMessage[] }) {
  const visible = React.useMemo(() => hideKickoffs([...messages]), [messages]);
  return (
    <div className="space-y-3">
      {visible.map((m, idx) => {
        if (m.hidden) return null;
        if (m.role === "user") {
          return (
            <UserBubble key={idx} content={m.content} createdAt={m.createdAt} editable={false} />
          );
        }
        return (
          <div key={idx} className="flex">
            <AgentBubble
              msg={m}
              streaming={false}
              renderToolCall={renderToolCall}
              isStepCall={isStepCall}
              stepKind={stepKind}
              className="max-w-full"
            />
          </div>
        );
      })}
    </div>
  );
}
