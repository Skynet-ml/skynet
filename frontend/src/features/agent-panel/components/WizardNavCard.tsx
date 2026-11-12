"use client";

import * as React from "react";

import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { StepRow } from "@/shared/ui/agent/activity-steps";
import { ArrowLeft, ArrowRight, type Icon } from "@/shared/ui/icons";
import type { AgentToolCall } from "@/shared/ui/agent/types";

import { isLongText } from "../lib/wizard-write";

import { savedNote } from "./WizardNoteCard";

// The stepper's own names, so "Back to …" reads like the rail the user sees.
const SCREEN_STAGE: Record<string, MessageKey> = {
  brief: "submit.v2.stage.task" as MessageKey,
  artifact: "submit.v2.stage.task" as MessageKey,
  source: "submit.v2.stage.inputs" as MessageKey,
  plan: "submit.v2.stage.optimizer" as MessageKey,
  optimizer: "submit.v2.stage.optimizer" as MessageKey,
  models: "submit.v2.stage.optimizer" as MessageKey,
  budget: "submit.v2.stage.optimizer" as MessageKey,
  metric: "submit.v2.stage.metric" as MessageKey,
  check: "submit.v2.stage.metric" as MessageKey,
  schedule: "submit.v2.stage.metric" as MessageKey,
  overview: "submit.v2.stage.metric" as MessageKey,
};

// StepRow draws the icon with fixed classes; these flip in RTL like the wizard's own arrows.
const Forward = ((props: React.ComponentProps<typeof ArrowRight>) => (
  <ArrowRight {...props} className={cn(props.className, "rtl:-scale-x-100")} />
)) as unknown as Icon;
const Backward = ((props: React.ComponentProps<typeof ArrowLeft>) => (
  <ArrowLeft {...props} className={cn(props.className, "rtl:-scale-x-100")} />
)) as unknown as Icon;

function argText(call: AgentToolCall, key: string): string {
  const args = call.payload?.arguments;
  const value = args && typeof args === "object" ? (args as Record<string, unknown>)[key] : null;
  return typeof value === "string" ? value.trim() : "";
}

/** A refused move comes back as a plain sentence instead of a ``screen_nav`` patch. */
function refusal(call: AgentToolCall): string {
  const result = call.payload?.result;
  return typeof result === "string" ? result.trim() : "";
}

function NavRow({
  icon,
  label,
  text,
  failed,
  status,
}: {
  icon: Icon;
  label: string;
  text: string;
  failed: boolean;
  status: AgentToolCall["status"];
}) {
  return (
    <StepRow
      icon={icon}
      label={label}
      detail={
        text && (
          <span dir="auto" title={text}>
            {text}
          </span>
        )
      }
      status={failed ? "error" : status}
      errorLabel={failed ? label : undefined}
    >
      {isLongText(text) ? (
        <p className="whitespace-pre-wrap break-words" dir="auto">
          {text}
        </p>
      ) : null}
    </StepRow>
  );
}

/**
 * The setup agent moving the wizard on, as a receipt: one step line with the
 * note it kept for later screens. A refused move names what still blocks it.
 */
export function WizardContinueCard({ call }: { call: AgentToolCall }) {
  const refused = refusal(call);
  if (refused || call.status === "error") {
    // The tool's last sentence is an instruction to the agent, not news for the user.
    const problems = refused
      .replace(/^Can't continue yet:\s*/, "")
      .replace(/\.\s*Fix this with the user, then try again\.?$/, "");
    return (
      <NavRow icon={Forward} label={msg("agent.screen_nav.blocked")} text={problems} failed status={call.status} />
    );
  }
  return (
    <NavRow
      icon={Forward}
      label={msg("agent.screen_nav.continued")}
      text={argText(call, "note") ? savedNote(call) : ""}
      failed={false}
      status={call.status}
    />
  );
}

/**
 * The setup agent sending the user back to an earlier screen, as a receipt:
 * the screen's step name and what the user wants changed there.
 */
export function WizardBackCard({ call }: { call: AgentToolCall }) {
  const refused = refusal(call);
  if (refused || call.status === "error") {
    return (
      <NavRow icon={Backward} label={msg("agent.screen_nav.back_failed")} text={refused} failed status={call.status} />
    );
  }
  const stage = SCREEN_STAGE[argText(call, "screen_id")];
  return (
    <NavRow
      icon={Backward}
      label={stage ? formatMsg("agent.screen_nav.back", { p1: msg(stage) }) : msg("agent.screen_nav.back_running")}
      text={argText(call, "request")}
      failed={false}
      status={call.status}
    />
  );
}
