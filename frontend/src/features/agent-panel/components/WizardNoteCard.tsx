"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { StepRow } from "@/shared/ui/agent/activity-steps";
import { PushPin } from "@/shared/ui/icons";
import type { AgentToolCall } from "@/shared/ui/agent/types";

import { isLongText } from "../lib/wizard-write";

/**
 * The note as the backend stored it (the newest of ``screen_notes``), so the
 * line shows what later screens read, not what the agent sent.
 */
export function savedNote(call: AgentToolCall): string {
  const result = call.payload?.result;
  const state =
    result && typeof result === "object" ? (result as Record<string, unknown>).wizard_state : null;
  const notes =
    state && typeof state === "object" ? (state as Record<string, unknown>).screen_notes : null;
  const last = Array.isArray(notes) ? notes[notes.length - 1] : null;
  const note = last && typeof last === "object" ? (last as Record<string, unknown>).note : null;
  return typeof note === "string" ? note.trim() : "";
}

/**
 * What the setup agent kept for a later screen, as a receipt: one step line
 * holding the note in the user's own words. A note too long for the line
 * opens under it. A refused call shows why.
 */
export function WizardNoteCard({ call }: { call: AgentToolCall }) {
  const note = savedNote(call);
  // The tool refuses an empty note with a plain sentence instead of a patch.
  const refusal = typeof call.payload?.result === "string" ? call.payload.result.trim() : "";
  const failed = call.status === "error" || (!note && Boolean(refusal));
  const shown = note || refusal;
  if (!shown) return null;
  return (
    <StepRow
      icon={PushPin}
      label={msg("agent.note.title")}
      detail={
        <span dir="auto" title={shown}>
          {shown}
        </span>
      }
      status={failed ? "error" : call.status}
      errorLabel={failed ? msg("agent.note.title") : undefined}
    >
      {isLongText(shown) ? (
        <p className="whitespace-pre-wrap break-words" dir="auto">
          {shown}
        </p>
      ) : null}
    </StepRow>
  );
}
