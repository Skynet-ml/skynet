"use client";

import * as React from "react";

import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { diffHunks, lineDiff, wordDiff, type DiffLine } from "@/shared/lib/line-diff";
import { OPTIMIZER_NAMES } from "@/shared/lib/optimizer-names";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import type { OptimizerChoice } from "@/shared/types/unified-run";
import { StepRow } from "@/shared/ui/agent/activity-steps";
import { toggleLabelKey } from "@/shared/ui/agent/collapse-rules";
import { ArrowRight, FileCode, MagicWand } from "@/shared/ui/icons";
import type { AgentToolCall } from "@/shared/ui/agent/types";

import { useWizardStateOptional } from "../hooks/use-wizard-state";
import {
  CODE_FIELD_FILES,
  codeChangeStats,
  fieldValueText,
  isLongText,
  listChange,
  wizardWriteChanges,
  writeSummary,
  type WizardFieldChange,
} from "../lib/wizard-write";

const ADDED_TONE = "text-[#5A7247] dark:text-[#adc899]";
const ADDED_FILL = "bg-[#5A7247]/12 dark:bg-[#adc899]/12";
const REMOVED_FILL = "bg-destructive/10";
const OLD_VALUE = "text-muted-foreground line-through decoration-muted-foreground/60";

// The labels the wizard's screens and run summary use, so a change reads in
// the words the user saw there. A field with none shows its key.
const FIELD_LABELS: Readonly<Record<string, MessageKey>> = {
  job_name: "submit.v2.overview.row.name",
  job_description: "auto.features.agent.panel.lib.entry.row.literal.21",
  is_private: "submit.basics.privacy.label",
  task_objective: "submit.v2.task.objective_label",
  task_background: "submit.v2.task.background_label",
  artifact_kind: "submit.v2.task.artifact.kind_label",
  starting_version: "submit.v2.task.artifact.starting_label",
  repository: "submit.v2.repo.repository_label",
  data_mode: "submit.v2.inputs.source.title",
  staged_dataset_id: "storage.type.dataset",
  split_fractions: "submit.v2.overview.row.split",
  shuffle: "auto.features.submit.components.steps.summarystep.16",
  optimizer_choice: "submit.v2.stage.optimizer",
  model_config: "submit.v2.optimizer.models.task.label",
  primary_model: "submit.v2.optimizer.models.task.label",
  reflection_model_config: "submit.v2.optimizer.models.reflection.label",
  reflection_model: "submit.v2.optimizer.models.reflection.label",
  spending_limit_usd: "submit.budget.label",
  input_fields: "auto.features.agent.panel.lib.entry.row.literal.41",
  expected_field: "auto.features.submit.components.steps.summarystep.literal.7",
};

const MODEL_FIELDS = new Set(["model_config", "primary_model", "reflection_model_config", "reflection_model"]);
const ID_FIELDS = new Set(["staged_dataset_id", "source_dataset_id"]);
const CODE_LIKE_FIELDS = new Set(["repository", "expected_field", "data_mode"]);
const SPLIT_PARTS = ["train", "val", "test"] as const;

function fieldLabel(field: string): string {
  const key = FIELD_LABELS[field];
  return key ? msg(key) : field;
}

/** How one side of a change reads. */
type Shown =
  | { kind: "inline"; text: string; code?: boolean }
  | { kind: "text"; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "json"; text: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** A field's value as the wizard shows it: names over ids, money as money. */
function shownValue(field: string, value: unknown): Shown | null {
  if (fieldValueText(value) === null) return null;
  const locale = getActiveIntlLocale();
  if (typeof value === "boolean") {
    const key =
      field === "is_private"
        ? value
          ? "submit.basics.privacy.private"
          : "submit.basics.privacy.public"
        : value
          ? "auto.features.agent.panel.lib.entry.row.literal.50"
          : "auto.features.agent.panel.lib.entry.row.literal.51";
    return { kind: "inline", text: msg(key) };
  }
  if (typeof value === "number") {
    const format =
      field === "spending_limit_usd"
        ? new Intl.NumberFormat(locale, { style: "currency", currency: "USD" })
        : new Intl.NumberFormat(locale);
    return { kind: "inline", text: format.format(value) };
  }
  if (MODEL_FIELDS.has(field) && isRecord(value) && typeof value.name === "string") {
    return { kind: "inline", text: value.name, code: true };
  }
  if (field === "split_fractions" && isRecord(value)) {
    const pct = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 1 });
    const parts = SPLIT_PARTS.flatMap((part) =>
      typeof value[part] === "number"
        ? [`${msg(`submit.split.label_${part}`)} ${pct.format(value[part] as number)}`]
        : [],
    );
    if (parts.length) return { kind: "inline", text: parts.join(" · ") };
  }
  if (typeof value === "string") {
    if (field === "optimizer_choice") {
      const name =
        value === "auto"
          ? msg("submit.v2.optimizer.option.auto.label")
          : OPTIMIZER_NAMES[value as Exclude<OptimizerChoice, "auto">];
      return { kind: "inline", text: name ?? value };
    }
    if (field === "artifact_kind" && (value === "dspy_program" || value === "repo")) {
      const key =
        value === "repo" ? "submit.blackbox.repo.kind_repo" : "submit.v2.task.artifact.kind.dspy_program";
      return { kind: "inline", text: msg(key) };
    }
    if (ID_FIELDS.has(field)) return { kind: "inline", text: value.slice(0, 8), code: true };
    if (isLongText(value)) return { kind: "text", text: value };
    return { kind: "inline", text: value, code: CODE_LIKE_FIELDS.has(field) };
  }
  if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
    return { kind: "list", items: value };
  }
  return { kind: "json", text: fieldValueText(value) ?? "" };
}

/**
 * What the setup agent wrote into the wizard, as a receipt: one step line
 * naming the changed settings, which opens to each setting's old and new
 * value in the wizard's own words, and each code file's diff. A write that
 * changed nothing shows nothing.
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
  const fields = changes.filter((change) => !CODE_FIELD_FILES[change.field]);
  const files = changes.filter((change) => CODE_FIELD_FILES[change.field]);
  return (
    <StepRow
      icon={MagicWand}
      label={msg("agent.wizard_write.title")}
      detail={<bdi>{writeSummary(changes, fieldLabel)}</bdi>}
      status={call.status}
    >
      {fields.length > 0 && (
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-4 gap-y-2">
          {fields.map((change) => (
            <FieldChange key={change.field} change={change} />
          ))}
        </dl>
      )}
      {files.map((change) => (
        <CodeChange key={change.field} file={CODE_FIELD_FILES[change.field]!} change={change} />
      ))}
    </StepRow>
  );
}

function FieldChange({ change }: { change: WizardFieldChange }) {
  const before = shownValue(change.field, change.before);
  const after = shownValue(change.field, change.after);
  const label = (
    <dt className="text-muted-foreground" dir="auto">
      {fieldLabel(change.field)}
    </dt>
  );
  if (!isInlineOrEmpty(before) || !isInlineOrEmpty(after)) {
    return <WideChange label={label} before={before} after={after} />;
  }
  return (
    <>
      {label}
      <dd className="flex min-w-0 flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
        {before && (
          <>
            <span className="sr-only">{msg("agent.wizard_write.before")}</span>
            <InlineValue shown={before} className={OLD_VALUE} />
            <ArrowRight
              className="size-3 shrink-0 self-center text-muted-foreground/70 rtl:-scale-x-100"
              aria-hidden
            />
            <span className="sr-only">{msg("agent.wizard_write.after")}</span>
          </>
        )}
        {after ? (
          <InlineValue shown={after} className="font-medium text-foreground" />
        ) : (
          <span className="italic text-muted-foreground">{msg("agent.wizard_write.empty")}</span>
        )}
      </dd>
    </>
  );
}

type InlineShown = Extract<Shown, { kind: "inline" }>;

function isInlineOrEmpty(shown: Shown | null): shown is InlineShown | null {
  return shown === null || shown.kind === "inline";
}

// A passage, a list or a structure takes the full width under its label.
function WideChange({
  label,
  before,
  after,
}: {
  label: React.ReactNode;
  before: Shown | null;
  after: Shown | null;
}) {
  return (
    <div className="col-span-2 flex min-w-0 flex-col gap-1">
      {label}
      <dd className="min-w-0">
        {before?.kind === "list" || after?.kind === "list" ? (
          <ListChange
            before={before?.kind === "list" ? before.items : []}
            after={after?.kind === "list" ? after.items : []}
          />
        ) : before?.kind === "json" || after?.kind === "json" ? (
          <DiffBlock lines={diffHunks(lineDiff(textOf(before), textOf(after)))} />
        ) : (
          <TextChange before={textOf(before)} after={textOf(after)} />
        )}
      </dd>
    </div>
  );
}

function textOf(shown: Shown | null): string {
  if (!shown) return "";
  return shown.kind === "list" ? shown.items.join("\n") : shown.text;
}

function InlineValue({
  shown,
  className,
}: {
  shown: InlineShown;
  className?: string;
}) {
  if (shown.code) {
    return (
      <bdi dir="ltr" className={cn("break-all font-mono text-[0.6875rem]", className)}>
        {shown.text}
      </bdi>
    );
  }
  return (
    <span dir="auto" className={cn("break-words", className)}>
      {shown.text}
    </span>
  );
}

function ListChange({ before, after }: { before: string[]; after: string[] }) {
  return (
    <ul className="flex flex-wrap gap-1">
      {listChange(before, after).map(({ item, state }) => (
        <li
          key={`${state}-${item}`}
          dir="ltr"
          className={cn(
            "rounded-md px-1.5 py-0.5 font-mono text-[0.6875rem]",
            state === "same" && "bg-muted/70 text-foreground/85",
            state === "add" && cn(ADDED_FILL, ADDED_TONE),
            state === "del" && cn(REMOVED_FILL, "text-destructive line-through"),
          )}
        >
          {state === "del" && <span className="sr-only">{msg("agent.wizard_write.before")}</span>}
          {item}
        </li>
      ))}
    </ul>
  );
}

// Past this many characters a passage folds to a few lines behind a toggle,
// so a long objective never turns the step into a wall of text.
const FOLD_CHARS = 280;

function FoldToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className="cursor-pointer rounded-sm text-[0.6875rem] font-medium text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
    >
      {msg(toggleLabelKey(open))}
    </button>
  );
}

function TextChange({ before, after }: { before: string; after: string }) {
  const [open, setOpen] = React.useState(false);
  const parts = React.useMemo(() => (before && after ? wordDiff(before, after) : []), [before, after]);
  const changed = parts.reduce((sum, part) => sum + (part.type === "same" ? 0 : part.text.length), 0);
  // A rewrite reads better as the new text than as a field of struck words.
  const rewrite = parts.length === 0 || changed > 0.6 * (before.length + after.length);
  const foldable = Math.max(before.length, after.length) > FOLD_CHARS;
  const clamp = foldable && !open;
  return (
    <div className="flex flex-col items-start gap-1">
      {rewrite && before && (
        <p dir="auto" className={cn("whitespace-pre-wrap break-words", OLD_VALUE, clamp && "line-clamp-2")}>
          <span className="sr-only">{msg("agent.wizard_write.before")}</span>
          {before}
        </p>
      )}
      <p
        dir="auto"
        className={cn("whitespace-pre-wrap break-words leading-relaxed", clamp && "line-clamp-5")}
      >
        {rewrite ? (
          after ? (
            <>
              {before && <span className="sr-only">{msg("agent.wizard_write.after")}</span>}
              <span className="text-foreground">{after}</span>
            </>
          ) : (
            <span className="italic text-muted-foreground">{msg("agent.wizard_write.empty")}</span>
          )
        ) : (
          parts.map((part, i) =>
            part.type === "same" ? (
              <span key={i} className="text-foreground/85">
                {part.text}
              </span>
            ) : part.type === "add" ? (
              <ins key={i} className={cn("rounded-[3px] no-underline", ADDED_FILL, ADDED_TONE)}>
                {part.text}
              </ins>
            ) : (
              <del key={i} className={cn("rounded-[3px] text-destructive", REMOVED_FILL)}>
                {part.text}
              </del>
            ),
          )
        )}
      </p>
      {foldable && <FoldToggle open={open} onToggle={() => setOpen((v) => !v)} />}
    </div>
  );
}

// A diff shows this many rows before it folds; it grows on demand instead of
// scrolling inside the chat.
const FOLD_ROWS = 14;

function DiffBlock({ lines }: { lines: DiffLine[] }) {
  const [open, setOpen] = React.useState(false);
  const foldable = lines.length > FOLD_ROWS;
  const shown = foldable && !open ? lines.slice(0, FOLD_ROWS) : lines;
  return (
    <div className="flex flex-col items-start gap-1">
      <pre
        dir="ltr"
        className="w-full overflow-hidden rounded-md border border-border/50 bg-muted/25 py-1 font-mono text-[0.6875rem]/[1.6] text-foreground/85"
      >
        {shown.map((line, i) =>
          line.type === "gap" ? (
            <div key={i} className="select-none px-2.5 text-muted-foreground/70">
              {formatMsg("optimization.blackbox.repo.browser.folded_lines", { count: line.count })}
            </div>
          ) : (
            <div
              key={i}
              className={cn(
                "flex gap-2 whitespace-pre-wrap break-words px-2.5",
                line.type === "add" && cn(ADDED_FILL, ADDED_TONE),
                line.type === "del" && cn(REMOVED_FILL, "text-destructive"),
              )}
            >
              <span className="w-2 shrink-0 select-none opacity-60" aria-hidden>
                {line.type === "add" ? "+" : line.type === "del" ? "−" : ""}
              </span>
              <span className="min-w-0">{line.text || " "}</span>
            </div>
          ),
        )}
      </pre>
      {foldable && <FoldToggle open={open} onToggle={() => setOpen((v) => !v)} />}
    </div>
  );
}

function CodeChange({ file, change }: { file: string; change: WizardFieldChange }) {
  const lines = React.useMemo(
    () => diffHunks(lineDiff(fieldValueText(change.before) ?? "", fieldValueText(change.after) ?? "")),
    [change.before, change.after],
  );
  const { added, removed } = codeChangeStats(change);
  return (
    <div className="flex flex-col gap-1.5">
      <span className="flex items-center gap-1.5 font-mono text-[0.6875rem]" dir="ltr">
        <FileCode className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <span className="text-foreground">{file}</span>
        <span className={cn("tabular-nums", ADDED_TONE)}>+{added}</span>
        <span className="tabular-nums text-destructive">−{removed}</span>
      </span>
      <DiffBlock lines={lines} />
    </div>
  );
}
