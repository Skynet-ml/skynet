"use client";

import * as React from "react";

import { formatMsg, msg } from "@/shared/lib/messages";
import { diffHunks, lineDiff } from "@/shared/lib/line-diff";
import { cn } from "@/shared/lib/utils";
import { StepRow } from "@/shared/ui/agent/activity-steps";
import { MagicWand } from "@/shared/ui/icons";
import type { AgentToolCall } from "@/shared/ui/agent/types";

import { useWizardStateOptional } from "../hooks/use-wizard-state";
import {
  CODE_FIELD_FILES,
  codeChangeStats,
  fieldValueText,
  wizardWriteChanges,
  writeSummary,
  type WizardFieldChange,
} from "../lib/wizard-write";

const ADDED_TONE = "text-[#5A7247] dark:text-[#adc899]";

/**
 * What the setup agent wrote into the wizard, as one collapsed step line:
 * the changed fields, and each code file with its line counts. Opening it
 * shows each field's old and new value and each code file's diff. A write
 * that changed nothing shows nothing.
 */
export function WizardWriteCard({ call }: { call: AgentToolCall }) {
  const liveBefore = useWizardStateOptional()?.writeBefore[call.id];
  const changes = wizardWriteChanges(call, liveBefore);
  if (call.status === "error") {
    const result = call.payload?.result;
    const reason = typeof result === "string" ? result.trim() : "";
    return (
      <StepRow
        icon={MagicWand}
        label={msg("agent.wizard_write.failed")}
        status="error"
        errorLabel={msg("agent.wizard_write.failed")}
      >
        {reason ? (
          <p className="whitespace-pre-wrap break-words" dir="auto">
            {reason}
          </p>
        ) : null}
      </StepRow>
    );
  }
  if (changes.length === 0) return null;
  return (
    <StepRow
      icon={MagicWand}
      label={msg("agent.wizard_write.title")}
      detail={
        <bdi dir="ltr" className="font-mono text-[0.75rem]">
          {writeSummary(changes)}
        </bdi>
      }
      status={call.status}
    >
      <ul className="divide-y divide-border/40 overflow-hidden rounded-lg border border-border/60 bg-card text-sm">
        {changes.map((change) => (
          <li key={change.field} className="flex flex-col gap-1.5 px-3 py-2">
            {CODE_FIELD_FILES[change.field] ? (
              <CodeChange file={CODE_FIELD_FILES[change.field]!} change={change} />
            ) : (
              <FieldChange change={change} />
            )}
          </li>
        ))}
      </ul>
    </StepRow>
  );
}

function FieldChange({ change }: { change: WizardFieldChange }) {
  const before = fieldValueText(change.before);
  const after = fieldValueText(change.after);
  return (
    <>
      <span className="font-mono text-xs text-muted-foreground" dir="ltr">
        {change.field}
      </span>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-1 text-xs">
        <dt className="text-muted-foreground">{msg("agent.wizard_write.before")}</dt>
        <dd className={cn("line-clamp-4 whitespace-pre-wrap break-words", before ? "text-muted-foreground line-through" : "italic text-muted-foreground")} dir="auto">
          {before ?? msg("agent.wizard_write.empty")}
        </dd>
        <dt className="text-muted-foreground">{msg("agent.wizard_write.after")}</dt>
        <dd className={cn("line-clamp-6 whitespace-pre-wrap break-words", !after && "italic text-muted-foreground")} dir="auto">
          {after ?? msg("agent.wizard_write.empty")}
        </dd>
      </dl>
    </>
  );
}

function CodeChange({ file, change }: { file: string; change: WizardFieldChange }) {
  const lines = React.useMemo(
    () => lineDiff(fieldValueText(change.before) ?? "", fieldValueText(change.after) ?? ""),
    [change.before, change.after],
  );
  const hunks = React.useMemo(() => diffHunks(lines), [lines]);
  const { added, removed } = codeChangeStats(change);
  return (
    <>
      <span className="flex items-center gap-2 font-mono text-xs" dir="ltr">
        <span className="text-foreground">{file}</span>
        <span className={ADDED_TONE}>+{added}</span>
        <span className="text-destructive">−{removed}</span>
      </span>
      <pre
        dir="ltr"
        className="max-h-64 overflow-auto rounded-md bg-muted/30 font-mono text-[0.6875rem]/[1.55] text-foreground/85"
      >
        {hunks.map((line, i) =>
          line.type === "gap" ? (
            <div key={i} className="select-none px-2 text-muted-foreground/70">
              {"⋯ "}
              {formatMsg("optimization.blackbox.repo.browser.folded_lines", { count: line.count })}
            </div>
          ) : (
            <div
              key={i}
              className={cn(
                "whitespace-pre-wrap break-words px-2",
                line.type === "add" && cn("bg-[#5A7247]/10", ADDED_TONE),
                line.type === "del" && "bg-destructive/10 text-destructive",
              )}
            >
              <span className="select-none opacity-60">
                {line.type === "add" ? "+ " : line.type === "del" ? "− " : "  "}
              </span>
              {line.text}
            </div>
          ),
        )}
      </pre>
    </>
  );
}
