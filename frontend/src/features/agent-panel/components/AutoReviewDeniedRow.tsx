"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { CircleNotch, Shield } from "@/shared/ui/icons";

import { getToolMeta } from "../lib/tool-meta";
import type { PendingApprovalPayload } from "../lib/types";

interface AutoReviewDeniedRowProps {
  payload: PendingApprovalPayload;
  onResolve: (approved: boolean) => void | Promise<void>;
}

const action =
  "h-6 shrink-0 cursor-pointer rounded-md px-2 text-[0.75rem] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring disabled:cursor-default disabled:opacity-60";

/**
 * The quiet row for a call the auto-review stopped (SKY-706), after Codex:
 * "Auto-review denied", the action and the reason. "Approve anyway" runs the
 * call through the normal confirm; "Keep denied" tells the agent it did not run.
 */
export function AutoReviewDeniedRow({ payload, onResolve }: AutoReviewDeniedRowProps) {
  const [busy, setBusy] = React.useState<"approve" | "deny" | null>(null);
  const handle = async (approved: boolean) => {
    if (busy) return;
    setBusy(approved ? "approve" : "deny");
    try {
      await onResolve(approved);
    } finally {
      setBusy(null);
    }
  };
  const spinner = <CircleNotch className="size-3 animate-spin motion-reduce:animate-none" aria-hidden="true" />;

  return (
    <div role="group" aria-label={msg("agent.approval.auto_review_denied")} className="py-1 text-[0.8125rem] text-muted-foreground">
      <div className="flex w-full min-w-0 items-center gap-2">
        <Shield className="size-3.5 shrink-0 text-destructive" aria-hidden="true" />
        <span className="shrink-0">{msg("agent.approval.auto_review_denied")}</span>
        <span className="min-w-0 truncate text-foreground/70">{getToolMeta(payload.tool).title}</span>
        <span className="ms-auto flex shrink-0 items-center gap-1">
          <button type="button" className={action} disabled={busy !== null} onClick={() => handle(false)}>
            {busy === "deny" ? spinner : msg("agent.approval.keep_denied")}
          </button>
          <button type="button" className={action} disabled={busy !== null} onClick={() => handle(true)}>
            {busy === "approve" ? spinner : msg("agent.approval.approve_anyway")}
          </button>
        </span>
      </div>
      {payload.auto_review?.reason && (
        <p className="ps-5.5 pt-0.5 text-[0.75rem]" dir="auto">
          {payload.auto_review.reason}
        </p>
      )}
    </div>
  );
}
