"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { StepRow } from "@/shared/ui/agent/activity-steps";
import type { Icon } from "@/shared/ui/icons";
import type { AgentToolCall } from "@/shared/ui/agent/types";

// Phosphor has no hook; this one follows its bold 256-unit grid so it sits
// level with the other step icons.
const HookIcon = React.forwardRef<SVGSVGElement, React.SVGProps<SVGSVGElement>>(function HookIcon(props, ref) {
  return (
    <svg
      ref={ref}
      viewBox="0 0 256 256"
      fill="none"
      stroke="currentColor"
      strokeWidth={24}
      strokeLinecap="round"
      strokeLinejoin="round"
      data-hook-icon=""
      {...props}
    >
      <circle cx="148" cy="44" r="20" />
      <path d="M148 64v104a48 48 0 0 1-96 0v-28l28 28" />
    </svg>
  );
}) as unknown as Icon;

// A hook that fired this moment swings once as it catches; one restored from
// a saved chat stays still.
const FRESH_MS = 4000;

function readRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

/**
 * The reply hook: the runtime held back a reply that left the user with
 * nothing to do and sent the agent feedback, as Claude Code shows a Stop
 * hook. One neutral line says why; it opens to the exact feedback the agent
 * got and the reply it replaced.
 */
export function TurnCheckCard({ call }: { call: AgentToolCall }) {
  const [fresh] = React.useState(() => call.endedAt != null && Date.now() - call.endedAt < FRESH_MS);
  const held = readRecord(call.payload?.arguments).reply;
  const reply = typeof held === "string" ? held.trim() : "";
  const result = readRecord(call.payload?.result);
  const feedback = typeof result.feedback === "string" ? result.feedback.trim() : "";
  const detail =
    Array.isArray(result.missing) && result.missing.length > 0
      ? msg("agent.turn_check.ask_missing")
      : msg("agent.turn_check.move_or_ask");
  return (
    <div className={fresh ? "agent-hook-fired" : undefined}>
      <StepRow
        icon={HookIcon}
        label={msg("agent.turn_check.title")}
        detail={<span title={detail}>{detail}</span>}
        status={call.status}
      >
        {feedback || reply ? (
          <>
            {feedback ? (
              <Section label={msg("agent.turn_check.feedback")}>
                {/* The agent is always told in English, whatever the UI language. */}
                <p className="whitespace-pre-wrap break-words" dir="ltr">
                  {feedback}
                </p>
              </Section>
            ) : null}
            {reply ? (
              <Section label={msg("agent.turn_check.held_back")}>
                <p
                  className="whitespace-pre-wrap break-words line-through decoration-muted-foreground/40"
                  dir="auto"
                >
                  {reply}
                </p>
              </Section>
            ) : null}
          </>
        ) : null}
      </StepRow>
    </div>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-[0.6875rem]/[1.55] font-medium text-muted-foreground/70">{label}</div>
      {children}
    </div>
  );
}
