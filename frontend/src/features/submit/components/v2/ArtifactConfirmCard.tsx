"use client";

import { LazyCodeEditor as CodeEditor } from "@/shared/ui/lazy-code-editor";
import { Textarea } from "@/shared/ui/primitives/textarea";
import { PingDot } from "@/shared/ui/ping-dot";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { getActiveMessages } from "@/shared/lib/runtime-messages";
import { cn } from "@/shared/lib/utils";
import type { ArtifactKind } from "@/shared/types/unified-run";

import { detectLanguage, looksLikeCode } from "../../lib/seed-format";

type MessageParams = Record<string, string | number>;

/**
 * A wizard v2 string, taking its plainer `<key>.guided` sibling for guided
 * users when the catalog has one.
 */
export function v2Msg(key: string, guided: boolean, params?: MessageParams): string {
  const guidedKey = `${key}.guided`;
  const chosen = guided && getActiveMessages()[guidedKey as MessageKey] ? guidedKey : key;
  return msg(chosen as MessageKey, params);
}

type StartingVersion = string | Record<string, string> | null;

export interface ArtifactConfirmCardProps {
  kind: ArtifactKind | null;
  startingVersion: StartingVersion;
  onStartingVersionChange: (value: string | Record<string, string>) => void;
  rationale: string;
  /** An i18n key naming what is missing, or null when the card is complete. */
  issue: string | null;
  /** The setup agent is writing the starting version; the editor holds until it lands. */
  writing: boolean;
  guided: boolean;
}

/**
 * What the setup agent derived: the starting version of the DSPy program the
 * run optimizes. There is no confirm button: Continue moves on once nothing
 * is missing.
 */
export function ArtifactConfirmCard({
  kind,
  startingVersion,
  onStartingVersionChange,
  rationale,
  issue,
  writing,
  guided,
}: ArtifactConfirmCardProps) {
  const t = (key: string, params?: MessageParams) => v2Msg(key, guided, params);

  return (
    <section
      aria-labelledby="v2-artifact-title"
      className="rounded-2xl border border-border/60 bg-background/70"
    >
      <div className="flex flex-col gap-1 border-b border-border/40 px-4 py-3">
        <div className="flex items-center gap-2">
          <h4 id="v2-artifact-title" className="text-sm font-semibold text-foreground">
            {t("submit.v2.task.artifact.title")}
          </h4>
          {writing && <PingDot size="sm" tone="agent" />}
        </div>
        <p className="text-xs text-muted-foreground" dir="auto">
          {rationale.trim() || t("submit.v2.task.artifact.desc")}
        </p>
      </div>

      <div className="flex flex-col gap-4 px-4 py-4">
        <div id="v2-starting-version" className="flex flex-col gap-2">
          <span className="text-sm font-medium text-foreground">
            {t("submit.v2.task.artifact.starting_label")}
          </span>
          <StartingVersionEditor
            kind={kind}
            value={startingVersion}
            onChange={onStartingVersionChange}
            streaming={writing}
            guided={guided}
          />
        </div>
      </div>

      <p
        className={cn(
          "px-4 text-xs text-muted-foreground",
          issue && "border-t border-border/40 py-3",
        )}
        aria-live="polite"
      >
        {issue ? t(issue) : null}
      </p>
    </section>
  );
}

function StartingVersionEditor({
  kind,
  value,
  onChange,
  streaming,
  guided,
}: {
  kind: ArtifactKind | null;
  value: StartingVersion;
  onChange: (value: string | Record<string, string>) => void;
  streaming: boolean;
  guided: boolean;
}) {
  const t = (key: string, params?: MessageParams) => v2Msg(key, guided, params);

  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value);
    return (
      <div className="flex flex-col gap-3">
        {kind === "dspy_program" && (
          <p className="text-xs text-muted-foreground">
            {t("submit.v2.task.artifact.predictors_hint")}
          </p>
        )}
        {entries.map(([name, text]) => (
          <div key={name} className="overflow-hidden rounded-xl border border-border bg-background">
            <label
              htmlFor={`v2-part-${name}`}
              className="block truncate border-b border-border bg-muted/30 px-3 py-2 font-mono text-xs font-medium"
              dir="ltr"
              title={name}
            >
              {name}
            </label>
            <Textarea
              id={`v2-part-${name}`}
              value={text}
              onChange={(e) => onChange({ ...value, [name]: e.target.value })}
              rows={3}
              readOnly={streaming}
              dir="auto"
              className="rounded-none border-0 bg-transparent shadow-none focus-visible:ring-inset"
            />
          </div>
        ))}
      </div>
    );
  }

  const text = value ?? "";
  const isCode = kind === "dspy_program" || looksLikeCode(text);
  if (isCode) {
    return (
      <CodeEditor
        value={text}
        onChange={onChange}
        height="320px"
        language={detectLanguage(text) ?? "Python"}
        label={t("submit.v2.task.artifact.starting_label")}
        streaming={streaming}
        readOnly={streaming}
      />
    );
  }
  return (
    <Textarea
      id="v2-starting-text"
      aria-label={t("submit.v2.task.artifact.starting_label")}
      value={text}
      onChange={(e) => onChange(e.target.value)}
      placeholder={t("submit.v2.task.artifact.starting_placeholder")}
      rows={8}
      readOnly={streaming}
      dir="auto"
      className="min-h-40 font-mono text-sm"
    />
  );
}
