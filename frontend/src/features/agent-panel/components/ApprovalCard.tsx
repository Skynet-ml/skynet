"use client";

import { ApprovalChoices } from "./ApprovalChoices";
import { ApprovalShell, SEVERITY_STYLES } from "./ApprovalShell";
import { AutoReviewDeniedRow } from "./AutoReviewDeniedRow";
import { SecretRequestCard } from "./SecretRequestCard";
import { getToolMeta } from "../lib/tool-meta";
import type { ApprovalChoice, PendingApprovalPayload } from "../lib/types";

interface ApprovalCardProps {
  payload: PendingApprovalPayload;
  onResolve: (approved: boolean, choice?: ApprovalChoice) => void | Promise<void>;
  className?: string;
}

/**
 * Approval prompt rendered inline in the chat thread when the agent is
 * about to run a gated MCP tool. Translates the technical tool name and
 * arguments into Hebrew plain-language copy. The header shows the title
 * only and opens the raw arguments (SKY-799).
 */
export function ApprovalCard({ payload, onResolve, className }: ApprovalCardProps) {
  if (payload.tool === "request_secret") {
    return <SecretRequestCard payload={payload} onResolve={onResolve} className={className} />;
  }
  if (payload.auto_review?.denied) {
    return <AutoReviewDeniedRow payload={payload} onResolve={onResolve} className={className} />;
  }
  return <ToolApprovalCard payload={payload} onResolve={onResolve} className={className} />;
}

function ToolApprovalCard({ payload, onResolve, className }: ApprovalCardProps) {
  const meta = getToolMeta(payload.tool);
  return (
    <ApprovalShell
      icon={meta.icon}
      title={meta.title}
      entries={Object.entries(payload.arguments ?? {})}
      styles={SEVERITY_STYLES[meta.severity]}
      className={className}
    >
      <ApprovalChoices allowLabel={meta.confirmLabel} chatRule={payload.chat_rule === true} onResolve={onResolve} />
    </ApprovalShell>
  );
}
