"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { ArrowsClockwise, type Icon } from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";
import { CopyButton } from "@/shared/ui/copy-button";
import { looksLikeMarkdown } from "@/shared/lib/markdown-hint";

import { cn } from "@/shared/lib/utils";
import { StepRow } from "@/shared/ui/agent/activity-steps";
import type { AgentToolCall } from "@/shared/ui/agent/types";

import { isPlainObject } from "../lib/entry-format";
import { getToolMeta, getToolTitle } from "../lib/tool-meta";

import { EntryRow } from "./EntryRow";

// The feature barrel (and with it this row) sits in the app shell's static
// graph, so the markdown parser loads only once a result is opened.
const MessageMarkdown = dynamic(
  () => import("@/shared/ui/agent/message-markdown").then((m) => m.MessageMarkdown),
  { ssr: false },
);

interface ToolCallRowProps {
  call: AgentToolCall;
  isRetry?: boolean;
  /** One-liner (verb and target) that replaces ``call.reason`` in the collapsed line. */
  summary?: string | null;
  /** Override for the line's title. Defaults to ``TOOL_META.title``. */
  title?: string | null;
  /** Override for the line's icon. Defaults to ``TOOL_META.icon``. */
  icon?: Icon;
  /**
   * Replaces the default args/result body, e.g. a result table. Shown in the
   * same indented area so every tool opens the same way.
   */
  customBody?: React.ReactNode;
}

/**
 * One tool invocation as a quiet one-line step, as in Codex: the tool's icon
 * and title in muted text, the per-tool summary after it, the running step
 * shimmering and a failed one marked with a warning. Clicking opens the args
 * and result as plain indented text, no frame.
 */
export function ToolCallRow({
  call,
  isRetry = false,
  summary,
  title,
  icon,
  customBody,
}: ToolCallRowProps) {
  const meta = getToolMeta(call.tool);
  const derivedTitle = title ?? getToolTitle(call.tool);

  const payload = (call.payload ?? {}) as Record<string, unknown>;
  const args = isPlainObject(payload.arguments) ? payload.arguments : {};
  const result = payload.result;
  const argEntries = Object.entries(args);
  const hasArgs = argEntries.length > 0;
  const hasResult = result !== undefined && result !== null && result !== "";
  // SKY-1006: a running command's output, replaced by the result at tool_end.
  // A dropped stream marks the call failed with no result; keep what the user watched.
  const liveOutput =
    (call.status === "running" || call.status === "error") &&
    !hasResult &&
    typeof payload.liveOutput === "string"
      ? payload.liveOutput
      : "";

  const triggerText = summary ?? call.reason ?? null;
  const isError = call.status === "error";
  const showReasonInBody = call.reason && call.reason !== triggerText;
  const hasBody =
    customBody !== undefined || showReasonInBody || hasArgs || hasResult || call.status === "running";

  const row = (
    <StepRow
      icon={icon ?? meta.icon}
      label={derivedTitle}
      detail={triggerText && <span title={triggerText}>{triggerText}</span>}
      status={call.status}
      errorLabel={msg("auto.features.agent.panel.components.toolcallrow.literal.7")}
      marks={
        isRetry && (
          <ArrowsClockwise
            className="size-3 shrink-0 opacity-60"
            aria-label={msg("auto.features.agent.panel.components.toolcallrow.literal.2")}
          />
        )
      }
    >
      {hasBody && (
        <>
          {showReasonInBody && <div>{call.reason}</div>}
          {customBody !== undefined ? (
            customBody
          ) : (
            <>
              {hasArgs && (
                <Section label={msg("auto.features.agent.panel.components.toolcallrow.literal.3")}>
                  <dl className="space-y-1.5">
                    {argEntries.map(([k, v]) => (
                      <EntryRow key={k} argKey={k} value={v} />
                    ))}
                  </dl>
                </Section>
              )}
              {hasResult && (
                <Section
                  label={
                    isError
                      ? msg("auto.features.agent.panel.components.toolcallrow.literal.4")
                      : msg("auto.features.agent.panel.components.toolcallrow.literal.5")
                  }
                >
                  <ResultBody result={result} isError={isError} />
                </Section>
              )}
              {!hasArgs && !hasResult && !liveOutput && call.status === "running" && (
                <div className="italic">
                  {msg("auto.features.agent.panel.components.toolcallrow.2")}
                </div>
              )}
            </>
          )}
        </>
      )}
    </StepRow>
  );
  if (!liveOutput) return row;
  // The row starts closed, so the live text shows below its line, as the Codex running cell does.
  return (
    <>
      {row}
      <div className="pb-2 pe-1 ps-5.5 pt-0.5">
        <Section label={msg("agent.tool.live_output")}>
          <LiveOutput text={liveOutput} />
        </Section>
      </div>
    </>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-[0.6875rem]/[1.55] font-medium text-muted-foreground/70">
        {label}
      </div>
      {children}
    </div>
  );
}

function ResultBody({ result, isError }: { result: unknown; isError: boolean }) {
  if (typeof result === "string") {
    const trimmed = result.trim();
    if (!trimmed) {
      return <EmptyPlaceholder />;
    }
    if (!isError && looksLikeMarkdown(trimmed)) {
      return (
        <div className="max-h-52 overflow-y-auto rounded-md border border-border/40 bg-background/70 px-2.5 py-2 text-[0.75rem] leading-relaxed text-foreground/90">
          <MessageMarkdown content={trimmed} />
        </div>
      );
    }
    return (
      <div className="relative" dir="ltr">
        <pre
          className={cn(
            "whitespace-pre-wrap break-words font-mono text-[0.6875rem]/[1.55] max-h-52 overflow-y-auto rounded-md border p-2 pe-9",
            isError
              ? "border-[#9B2C1F]/20 dark:border-[#ec7764]/20 bg-[#FCEFEB]/60 dark:bg-[#463833]/60 text-[#7A1E13] dark:text-[#ffb2a0]"
              : "border-border/40 bg-background/70 text-foreground/90",
          )}
        >
          {trimmed}
        </pre>
        <CopyButton
          text={trimmed}
          ariaLabel={msg("auto.features.agent.panel.components.toolcallrow.literal.10")}
          copiedAriaLabel={msg("auto.features.agent.panel.components.toolcallrow.literal.9")}
          stopPropagation
          className="absolute end-1.5 top-1.5"
        />
      </div>
    );
  }

  if (isPlainObject(result)) {
    const entries = Object.entries(result);
    if (entries.length === 0) {
      return <EmptyPlaceholder />;
    }
    return (
      <dl className="space-y-1.5">
        {entries.map(([k, v]) => (
          <EntryRow key={k} argKey={k} value={v} />
        ))}
      </dl>
    );
  }

  if (Array.isArray(result)) {
    if (result.length === 0) {
      return <EmptyPlaceholder />;
    }
    return (
      <dl className="space-y-1.5">
        {result.slice(0, 20).map((item, idx) => (
          <EntryRow key={idx} argKey={`${idx + 1}`} value={item} />
        ))}
        {result.length > 20 && (
          <div className="text-[0.625rem] italic text-muted-foreground/60">
            {msg("auto.features.agent.panel.components.toolcallrow.3")}
            {result.length - 20}…
          </div>
        )}
      </dl>
    );
  }

  return (
    <div className="font-mono text-[0.6875rem]/[1.55] text-foreground/90" dir="ltr">
      {String(result)}
    </div>
  );
}

/** The live output tail; it follows new text unless the user scrolled up. */
function LiveOutput({ text }: { text: string }) {
  const ref = React.useRef<HTMLPreElement>(null);
  const follow = React.useRef(true);
  React.useLayoutEffect(() => {
    const el = ref.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
  }, [text]);
  return (
    <pre
      ref={ref}
      dir="ltr"
      data-testid="tool-live-output"
      onScroll={(e) => {
        const el = e.currentTarget;
        follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
      }}
      className="whitespace-pre-wrap break-words font-mono text-[0.6875rem]/[1.55] max-h-52 overflow-y-auto rounded-md border border-border/40 bg-background/70 p-2 text-foreground/90"
    >
      {text}
    </pre>
  );
}

function EmptyPlaceholder() {
  return <div className="text-[0.6875rem]/[1.55] text-muted-foreground/60">—</div>;
}
