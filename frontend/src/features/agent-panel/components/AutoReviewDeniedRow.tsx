"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { CircleNotch, Shield } from "@/shared/ui/icons";

import { ApprovalShell, SEVERITY_STYLES } from "./ApprovalShell";
import { getToolMeta } from "../lib/tool-meta";
import type { PendingApprovalPayload } from "../lib/types";

interface AutoReviewDeniedRowProps {
  payload: PendingApprovalPayload;
  onResolve: (approved: boolean) => void | Promise<void>;
  className?: string;
}

const STYLES = { ...SEVERITY_STYLES.info, iconBg: "bg-destructive/10", iconText: "text-destructive" };

/**
 * The card for a call the auto-review stopped (SKY-706, SKY-799). It uses the
 * approval card's frame. Key 1 keeps the call denied and tells the agent it did
 * not run; key 2 approves it anyway through the normal confirm.
 *
 * Args:
 *   payload: The pending call.
 *   onResolve: Called with the answer.
 *   className: Extra classes for the frame.
 *
 * Returns:
 *   The card element.
 */
export function AutoReviewDeniedRow({ payload, onResolve, className }: AutoReviewDeniedRowProps) {
  const [busy, setBusy] = React.useState<boolean | null>(null);
  const [focus, setFocus] = React.useState(0);
  const rows = React.useRef<Array<HTMLButtonElement | null>>([]);
  const options = [
    { approved: false, label: msg("agent.approval.keep_denied") },
    { approved: true, label: msg("agent.approval.approve_anyway") },
  ];
  const handle = async (approved: boolean) => {
    if (busy !== null) return;
    setBusy(approved);
    try {
      await onResolve(approved);
    } finally {
      setBusy(null);
    }
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (busy !== null) return;
    const n = Number(e.key);
    if (n === 1 || n === 2) {
      e.preventDefault();
      void handle(options[n - 1]!.approved);
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = 1 - focus;
      setFocus(next);
      rows.current[next]?.focus();
    }
  };

  return (
    <ApprovalShell
      icon={Shield}
      title={`${msg("agent.approval.auto_review_denied")}: ${getToolMeta(payload.tool).title}`}
      entries={Object.entries(payload.arguments ?? {})}
      styles={STYLES}
      className={className}
    >
      <div
        role="group"
        aria-label={msg("agent.approval.auto_review_denied")}
        onKeyDown={onKey}
        className="flex flex-col gap-0.5"
      >
        {options.map((option, i) => (
          <button
            key={option.label}
            ref={(el) => {
              rows.current[i] = el;
            }}
            type="button"
            disabled={busy !== null}
            onFocus={() => setFocus(i)}
            onClick={() => void handle(option.approved)}
            className={cn(
              "flex h-8 cursor-pointer items-center gap-2.5 rounded px-2 text-start text-[0.8125rem] outline-none transition-colors disabled:cursor-default disabled:opacity-60",
              focus === i ? "bg-accent text-foreground" : "text-foreground/85 hover:bg-accent/60",
            )}
          >
            <kbd className="flex size-5 shrink-0 items-center justify-center rounded border border-border font-sans text-[0.6875rem] tabular-nums text-muted-foreground">
              {i + 1}
            </kbd>
            <span className="min-w-0 truncate">
              {busy === option.approved ? (
                <CircleNotch className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
              ) : (
                option.label
              )}
            </span>
          </button>
        ))}
      </div>
    </ApprovalShell>
  );
}
