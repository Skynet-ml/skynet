"use client";

import * as React from "react";

import { formatMsg, msg } from "@/shared/lib/messages";
import { diffHunks, diffStats, lineDiff } from "@/shared/lib/line-diff";
import { cn } from "@/shared/lib/utils";
import { MagicWand } from "@/shared/ui/icons";
import type { AgentToolCall } from "@/shared/ui/agent/types";

import {
  CODE_FIELD_FILES,
  fieldValueText,
  wizardWriteChanges,
  type WizardFieldChange,
} from "../lib/wizard-write";

const ADDED_TONE = "text-[#5A7247] dark:text-[#adc899]";

/**
 * What the setup agent wrote into the wizard: each field with its old and
 * new value, and each code file as a diff of the lines it changed.
 */
export function WizardWriteCard({ call }: { call: AgentToolCall }) {
  const changes = wizardWriteChanges(call);
  if (changes.length === 0) return null;
  return (
    <section className="overflow-hidden rounded-xl border border-border/60 bg-card text-sm">
      <h4 className="flex items-center gap-1.5 border-b border-border/40 px-3 py-2 text-xs font-medium text-muted-foreground">
        <MagicWand className="size-3.5 shrink-0" aria-hidden="true" />
        {call.status === "error" ? msg("agent.wizard_write.failed") : msg("agent.wizard_write.title")}
      </h4>
      <ul className="divide-y divide-border/40">
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
    </section>
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
  const { added, removed } = diffStats(lines);
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
