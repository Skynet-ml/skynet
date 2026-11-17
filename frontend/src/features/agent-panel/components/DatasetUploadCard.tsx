"use client";

import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { Button } from "@/shared/ui/primitives/button";
import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Check, CircleNotch, FileXls, UploadSimple } from "@/shared/ui/icons";
import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { DATASET_UPLOAD_ACCEPT, parseDatasetFile } from "@/shared/lib/parse-dataset";

import { attachedFile, splitPastes, type AttachedFile } from "@/shared/ui/agent/pasted-text";
import type { AgentMessage, AgentToolCall } from "@/shared/ui/agent/types";
import { draftHasChip, putDatasetChip } from "../lib/dataset-chip";
import type { WizardState } from "../lib/types";
import type { ConfirmedLibraryDataset } from "./LibraryDatasetCard";

/** A read dataset file. It carries no column roles: the setup agent infers them (SKY-742). */
export interface ConfirmedDataset {
  fileName: string;
  rows: Array<Record<string, unknown>>;
  rowCount: number;
  columns: string[];
}

interface DatasetUploadCardProps {
  /** The agent tool call that requested the upload; absent when the user
   *  attaches proactively from the composer's + button. */
  call?: AgentToolCall;
  disabled?: boolean;
  alreadyConfirmed?: boolean;
  /** File already chosen via the OS picker; when set, the card skips the
   *  dropzone entirely and parses it straight away. */
  initialFile?: File | null;
  /** The picked file goes too, so the chat can keep its original for the viewer. */
  onConfirm: (confirmed: ConfirmedDataset, file: File) => void;
}

const DATASET_FILE_ACCEPT = DATASET_UPLOAD_ACCEPT;

// Rows and characters per row of an uploaded dataset that the agent's note shows.
const DATASET_NOTE_SAMPLE_ROWS = 3;
const DATASET_NOTE_ROW_CHARS = 300;

/** Build the chat note that tells the setup agent a file is attached, with its columns and first rows. */
export function datasetAttachedNote(dataset: ConfirmedDataset): string {
  const sampleLines = dataset.rows
    .slice(0, DATASET_NOTE_SAMPLE_ROWS)
    .map((row) => {
      // Cut by code point, so the cut never splits a surrogate pair.
      const chars = Array.from(JSON.stringify(row));
      const line = chars.slice(0, DATASET_NOTE_ROW_CHARS).join("");
      return chars.length > DATASET_NOTE_ROW_CHARS ? `${line}…` : line;
    })
    .join("\n");
  // The agent copies column names from this note, so it must carry no
  // RTL isolate marks (U+2066-U+2069) that formatMsg adds around Latin text.
  return formatMsg("auto.features.agent.panel.components.generalistpanel.dataset_attached_note", {
    p1: dataset.fileName,
    p2: dataset.rowCount,
    // A JSON list keeps column names that hold a comma unambiguous.
    p3: JSON.stringify(dataset.columns),
    p4: sampleLines,
  }).replace(/[\u2066-\u2069]/g, "");
}

/**
 * Build the turn override for a library dataset pick.
 *
 * The chat keeps an override on every later turn, so it carries no
 * ``columns_configured``: a later upload must be able to clear it.
 */
export function libraryPickOverride(picked: ConfirmedLibraryDataset): WizardState {
  return {
    source_dataset_id: picked.sourceDatasetId,
    dataset_ready: true,
    dataset_columns: picked.columns,
  };
}

/** The wizard writes an upload makes; the shared wizard context fits it. */
interface DatasetUploadWizard {
  setField: (key: keyof WizardState, value: WizardState[keyof WizardState], source?: "user") => void;
  clearField: (key: keyof WizardState) => void;
}

/**
 * Write an uploaded file into the wizard and build its staged event and turn override.
 *
 * Nothing here carries column roles or ``columns_configured``: the agent
 * infers the roles and its column-role tool sets both (SKY-742).
 *
 * Args:
 *   wizard: The shared wizard context, or null when no wizard is mounted.
 *   dataset: The read file.
 *   stagedDatasetId: The backend's staged id, or undefined when staging failed.
 *
 * Returns:
 *   The ``wizard:dataset-staged`` event detail, and the turn override
 *   (undefined without a staged id).
 */
export function applyDatasetUpload(
  wizard: DatasetUploadWizard | null,
  dataset: ConfirmedDataset,
  stagedDatasetId: string | undefined,
): {
  detail: {
    dataset: Array<Record<string, unknown>>;
    dataset_filename: string;
    staged_dataset_id: string | undefined;
    wizard_state: WizardState;
  };
  override: WizardState | undefined;
} {
  // A wizard mounted on /submit later hydrates the rows from the staged id
  // and reads the columns from the shared context, not from the
  // quota-fragile sessionStorage replay.
  if (stagedDatasetId && wizard) {
    wizard.setField("staged_dataset_id", stagedDatasetId, "user");
    wizard.setField("dataset_columns", dataset.columns, "user");
    wizard.setField("dataset_ready", true, "user");
  }
  // Roles set for an earlier file do not fit this one. The flag is cleared,
  // not set to false: roles the user then sets on the Data screen do not
  // write the flag, and a stale false would tell the agent to redo them.
  if (wizard) {
    wizard.clearField("input_fields");
    wizard.clearField("expected_field");
    wizard.clearField("columns_configured");
  }
  return {
    detail: {
      dataset: dataset.rows,
      dataset_filename: dataset.fileName,
      staged_dataset_id: stagedDatasetId,
      wizard_state: { dataset_ready: true, dataset_columns: dataset.columns },
    },
    // The override stays on every later turn, so a role in it would outlive
    // the agent's own column-role call.
    override: stagedDatasetId
      ? { staged_dataset_id: stagedDatasetId, dataset_ready: true, dataset_columns: dataset.columns }
      : undefined,
  };
}

/** The options a chat send takes with its note: the message's files. */
type SendOptions = { media?: AgentMessage["media"] };

/**
 * Queue chat sends that run after the next render.
 *
 * Each send reads the wizard state of that render, so a field the caller
 * clears just before it does not reach the turn (SKY-742).
 *
 * Args:
 *   send: The chat send function.
 *
 * Returns:
 *   A function that queues one note, its turn override and its send options.
 */
export function useSendAfterRender(
  send: (note: string, override?: WizardState, options?: SendOptions) => void,
): (note: string, override?: WizardState, options?: SendOptions) => void {
  // A queue, so two sends in one tick both go out, in order.
  const [pending, setPending] = React.useState<
    Array<{ note: string; override?: WizardState; options?: SendOptions }>
  >([]);
  React.useEffect(() => {
    if (pending.length === 0) return;
    // Drop only what this effect sends: a send queued after this render
    // but before this effect is still in the queue.
    setPending((queue) => queue.slice(pending.length));
    for (const item of pending) send(item.note, item.override, item.options);
  }, [pending, send]);
  return React.useCallback(
    (note, override, options) => setPending((queue) => [...queue, { note, override, options }]),
    [],
  );
}

/** A confirmed dataset whose card waits in the draft; ``owner`` is the agent's tool call, if any. */
type DraftDataset = { chip: AttachedFile; owner?: string } & (
  | { kind: "upload"; dataset: ConfirmedDataset; staging: boolean; stagedId?: string }
  | { kind: "library"; picked: ConfirmedLibraryDataset }
);

/** Build the agent's context for a library pick: its name, row count and column roles. */
function libraryPickNote(picked: ConfirmedLibraryDataset): string {
  const mappingLine = picked.columns
    .map((c) => {
      const role = picked.columnRoles[c];
      if (role === "ignore") return null;
      return `- ${c} → ${role}`;
    })
    .filter(Boolean)
    .join("\n");
  return formatMsg("auto.features.agent.panel.components.generalistpanel.dataset_note", {
    p1: picked.name,
    p2: picked.rowCount,
    p3: mappingLine,
  });
}

/**
 * Hold a confirmed dataset in the composer draft until Send, as a PDF waits (SKY-780).
 *
 * A confirm puts one file card in the draft and stages the rows. Send
 * applies the dataset to the wizard and gives the turn override. A card the
 * user removes before Send changes nothing.
 *
 * Args:
 *   draft: The composer draft.
 *   setDraft: Sets the composer draft; it takes an update function.
 *   stage: Stages the rows on the backend; resolves to the staged id, or
 *     undefined when staging failed.
 *   wizard: The shared wizard context, or null when no wizard is mounted.
 *   onRelease: Gets the owner of a dataset whose card left the draft unsent
 *     (removed, or replaced by another dataset), so its tool card can reopen.
 *   onLeave: Gets the draft's files and the indexes of the ones a new card
 *     takes out, so their stored originals can go too.
 *
 * Returns:
 *   ``attach`` and ``attachLibrary`` put a card in the draft. ``holds``
 *   tells if a draft has the pending card. ``staging``
 *   tells if Send must wait for the rows. ``take`` applies the dataset of a
 *   draft on Send and returns its turn override, or null for no dataset.
 */
export function useDatasetDraft({
  draft,
  setDraft,
  stage,
  wizard,
  onRelease,
  onLeave,
}: {
  draft: string;
  setDraft: (update: (draft: string) => string) => void;
  stage: (dataset: ConfirmedDataset) => Promise<string | undefined>;
  wizard: DatasetUploadWizard | null;
  onRelease?: (owner: string) => void;
  onLeave?: (files: readonly AttachedFile[], leaving: ReadonlySet<number>) => void;
}): {
  attach: (dataset: ConfirmedDataset, owner?: string) => void;
  attachLibrary: (picked: ConfirmedLibraryDataset, owner?: string) => void;
  holds: (draft: string) => boolean;
  staging: (draft: string) => boolean;
  take: (draft: string) => { override: WizardState | undefined } | null;
} {
  const [pending, setPending] = React.useState<DraftDataset | null>(null);
  // Send reads the latest dataset in the same tick as a confirm or a staging end.
  const pendingRef = React.useRef<DraftDataset | null>(null);
  const update = React.useCallback((next: DraftDataset | null) => {
    pendingRef.current = next;
    setPending(next);
  }, []);

  const onReleaseRef = React.useRef(onRelease);
  React.useEffect(() => {
    onReleaseRef.current = onRelease;
  }, [onRelease]);

  const onLeaveRef = React.useRef(onLeave);
  React.useEffect(() => {
    onLeaveRef.current = onLeave;
  }, [onLeave]);

  const putCard = React.useCallback(
    (next: DraftDataset) => {
      const earlier = pendingRef.current;
      // The cards putDatasetChip takes out: the earlier card and each same
      // card, such as one a fork restored. A tool card's click runs after the
      // last render, so this draft is the one putDatasetChip changes.
      const files = splitPastes(draft).files;
      const same = (a: AttachedFile, b: AttachedFile) => a.name === b.name && a.text === b.text;
      const leaving = new Set(
        files.flatMap((f, i) =>
          same(f, next.chip) || (earlier && same(f, earlier.chip)) ? [i] : [],
        ),
      );
      if (leaving.size) onLeaveRef.current?.(files, leaving);
      update(next);
      setDraft((draft) => putDatasetChip(draft, next.chip, earlier?.chip ?? null));
      if (earlier?.owner) onReleaseRef.current?.(earlier.owner);
    },
    [draft, setDraft, update],
  );

  // A card the user removed from the draft drops its dataset.
  React.useEffect(() => {
    const current = pendingRef.current;
    if (!current || draftHasChip(draft, current.chip)) return;
    update(null);
    if (current.owner) onReleaseRef.current?.(current.owner);
  }, [draft, pending, update]);

  const attach = React.useCallback(
    (dataset: ConfirmedDataset, owner?: string) => {
      const next: DraftDataset = {
        kind: "upload",
        chip: attachedFile(dataset.fileName, datasetAttachedNote(dataset)),
        owner,
        dataset,
        staging: true,
      };
      putCard(next);
      // Staging is best-effort: with no staged id the agent gets a clear
      // 422 on submit, and the user can attach the file again.
      void stage(dataset)
        .catch(() => undefined)
        .then((stagedId) => {
          if (pendingRef.current === next) update({ ...next, staging: false, stagedId });
        });
    },
    [putCard, stage, update],
  );

  const attachLibrary = React.useCallback(
    (picked: ConfirmedLibraryDataset, owner?: string) =>
      putCard({
        kind: "library",
        chip: attachedFile(picked.name, libraryPickNote(picked)),
        owner,
        picked,
      }),
    [putCard],
  );

  const holds = React.useCallback((draft: string) => {
    const current = pendingRef.current;
    return Boolean(current && draftHasChip(draft, current.chip));
  }, []);

  const staging = React.useCallback(
    (draft: string) =>
      pending?.kind === "upload" && pending.staging && draftHasChip(draft, pending.chip),
    [pending],
  );

  const take = React.useCallback(
    (draft: string): { override: WizardState | undefined } | null => {
      const current = pendingRef.current;
      if (!current || !draftHasChip(draft, current.chip)) return null;
      update(null);
      if (current.kind === "library") {
        const { picked } = current;
        // By reference: the run reads the rows from ``source_dataset_id`` on
        // submit, so nothing stages.
        if (wizard) {
          wizard.setField("source_dataset_id", picked.sourceDatasetId, "user");
          wizard.setField("dataset_columns", picked.columns, "user");
          wizard.setField("dataset_ready", true, "user");
          wizard.setField("columns_configured", true, "user");
        }
        return { override: libraryPickOverride(picked) };
      }
      const { detail, override } = applyDatasetUpload(wizard, current.dataset, current.stagedId);
      // Persist before dispatch so the wizard can replay it after navigation;
      // the listener in use-unified-wizard reads and clears this key on mount.
      try {
        window.sessionStorage.setItem("wizard:staged-dataset", JSON.stringify(detail));
      } catch {
        // sessionStorage can throw under quota or private-mode policy; the
        // live event below still covers the same-page case.
      }
      window.dispatchEvent(new CustomEvent("wizard:dataset-staged", { detail }));
      return { override };
    },
    [update, wizard],
  );

  return React.useMemo(
    () => ({ attach, attachLibrary, holds, staging, take }),
    [attach, attachLibrary, holds, staging, take],
  );
}

export function DatasetUploadCard({
  call,
  disabled,
  alreadyConfirmed,
  initialFile,
  onConfirm,
}: DatasetUploadCardProps) {
  const reduceMotion = useReducedMotion();
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  const [parsed, setParsed] = React.useState<ConfirmedDataset | null>(null);
  const [parsing, setParsing] = React.useState(false);
  const [dragOver, setDragOver] = React.useState(false);
  const [parseError, setParseError] = React.useState<string | null>(null);
  const [confirmed, setConfirmed] = React.useState(!!alreadyConfirmed);
  // A new onConfirm on each parent render must not re-read the initial file.
  const onConfirmRef = React.useRef(onConfirm);
  React.useEffect(() => {
    onConfirmRef.current = onConfirm;
  }, [onConfirm]);
  // The drop zone and the initial file reach handleFile without the
  // disabled controls, so the read itself must refuse a disabled card.
  const disabledRef = React.useRef(disabled);
  React.useEffect(() => {
    disabledRef.current = disabled;
  }, [disabled]);

  React.useEffect(() => {
    if (alreadyConfirmed) setConfirmed(true);
  }, [alreadyConfirmed]);

  // Each read gets a number; a newer read, an unmount or a Strict Mode
  // remount drops an older one, so a closed card attaches nothing.
  const readRef = React.useRef(0);

  const handleFile = React.useCallback(async (file: File) => {
    if (disabledRef.current) return;
    const readId = ++readRef.current;
    setParsing(true);
    setParseError(null);
    try {
      const result = await parseDatasetFile(file);
      if (readId !== readRef.current) return;
      if (!result.columns.length) throw new Error("empty");
      const read: ConfirmedDataset = {
        fileName: file.name,
        rows: result.rows,
        rowCount: result.rowCount,
        columns: result.columns,
      };
      setParsed(read);
      setConfirmed(true);
      onConfirmRef.current(read, file);
    } catch {
      if (readId !== readRef.current) return;
      setParsed(null);
      setParseError(msg("auto.features.agent.panel.components.datasetuploadcard.parse_error"));
    } finally {
      if (readId === readRef.current) setParsing(false);
    }
  }, []);

  React.useEffect(() => {
    if (initialFile) void handleFile(initialFile);
    return () => {
      readRef.current += 1;
    };
  }, [initialFile, handleFile]);

  const onPickClick = React.useCallback(() => {
    inputRef.current?.click();
  }, []);

  const onInputChange = React.useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      e.target.value = "";
      if (file) await handleFile(file);
    },
    [handleFile],
  );

  const onDragOver = React.useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(true);
  }, []);
  const onDragLeave = React.useCallback(() => setDragOver(false), []);
  const onDrop = React.useCallback(
    async (e: React.DragEvent) => {
      e.preventDefault();
      setDragOver(false);
      const file = e.dataTransfer.files?.[0];
      if (file) await handleFile(file);
    },
    [handleFile],
  );

  const fade = reduceMotion
    ? { initial: false, animate: { opacity: 1 }, exit: { opacity: 0 } }
    : {
        initial: { opacity: 0, y: 4 },
        animate: { opacity: 1, y: 0 },
        exit: { opacity: 0, y: -4 },
        transition: { duration: 0.18, ease: [0.2, 0.8, 0.2, 1] as const },
      };

  return (
    <div className="w-full">
      <motion.div
        layout={!reduceMotion}
        className={cn(
          "relative overflow-hidden rounded-md border bg-card",
          "border-primary-fill/40",
          confirmed && "from-[#F4F0E8] dark:from-[#423b2f] to-[#EEE6D7] dark:to-[#464034] border-primary-fill/30",
        )}
      >
        <div className="px-4 pt-3.5 pb-2.5 border-b border-primary-fill/25 bg-white/40 dark:bg-white/5">
          <div className="flex items-start gap-2.5">
            <div
              className={cn(
                "shrink-0 size-7 rounded-full inline-flex items-center justify-center",
                "bg-primary-fill text-primary-foreground",
              )}
            >
              {confirmed ? <Check className="size-3.5" /> : <UploadSimple className="size-3.5" />}
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-[0.8125rem] font-semibold text-primary leading-tight">
                {confirmed
                  ? msg("auto.features.agent.panel.components.datasetuploadcard.attached")
                  : msg("auto.features.agent.panel.components.datasetuploadcard.title")}
              </div>
            </div>
          </div>
        </div>

        <input
          ref={inputRef}
          type="file"
          accept={DATASET_FILE_ACCEPT}
          className="hidden"
          onChange={onInputChange}
          disabled={disabled || parsing || confirmed}
        />

        <AnimatePresence mode="wait" initial={false}>
          {!parsed && !parsing && initialFile && parseError && (
            <motion.div key="pick-error" {...fade} className="px-4 py-3.5 space-y-2">
              <InlineErrorRow message={parseError} className="py-2" />
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={onPickClick}
                disabled={disabled}
                className="w-full"
              >
                <UploadSimple className="size-3.5" />
                {msg("auto.features.agent.panel.components.datasetuploadcard.replace")}
              </Button>
            </motion.div>
          )}

          {!parsed && !parsing && !initialFile && !confirmed && (
            <motion.div key="empty" {...fade} className="px-4 py-3.5">
              <button
                type="button"
                onClick={onPickClick}
                onDragOver={onDragOver}
                onDragEnter={onDragOver}
                onDragLeave={onDragLeave}
                onDrop={onDrop}
                disabled={disabled}
                className={cn(
                  "group flex h-[164px] w-full flex-col items-center justify-center gap-3 rounded-md border-2 border-dashed px-4",
                  "cursor-pointer bg-muted outline-none transition-colors duration-150",
                  "focus-visible:ring-2 focus-visible:ring-primary/45",
                  dragOver
                    ? "border-primary bg-primary-fill/10"
                    : "border-border hover:border-foreground/25",
                  "disabled:cursor-not-allowed disabled:opacity-50",
                )}
              >
                <div className="inline-flex size-10 items-center justify-center rounded-full bg-border text-muted-foreground">
                  <UploadSimple className="size-5" />
                </div>
                <div className="text-center">
                  <div className="text-sm font-medium text-primary">
                    {msg("auto.features.agent.panel.components.datasetuploadcard.drop_label")}
                  </div>
                </div>
              </button>
              {parseError && <InlineErrorRow message={parseError} className="mt-2 py-2" />}
            </motion.div>
          )}

          {parsing && (
            <motion.div
              key="parsing"
              {...fade}
              className="px-4 py-6 flex items-center justify-center gap-2 text-[0.75rem] text-[#6B5B4A] dark:text-[#cfbdaa]"
            >
              <CircleNotch className="size-3.5 animate-spin" />
              {msg("auto.features.agent.panel.components.datasetuploadcard.parsing")}
            </motion.div>
          )}

          {parsed && (
            <motion.div key="parsed" {...fade} className="px-4 py-3 space-y-3">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <FileXls className="size-3.5 text-primary shrink-0" />
                  <div className="min-w-0">
                    <div className="text-[0.75rem] font-medium text-primary truncate">
                      {parsed.fileName}
                    </div>
                    <div className="text-[0.6875rem] text-[#8A7866] dark:text-[#cdbaa7] tabular-nums leading-tight">
                      {parsed.rowCount}{" "}
                      {msg("auto.features.agent.panel.components.datasetuploadcard.rows_suffix")} ·{" "}
                      {parsed.columns.length}{" "}
                      {msg("auto.features.agent.panel.components.datasetuploadcard.columns_suffix")}
                    </div>
                  </div>
                </div>
              </div>
              <div className="flex flex-wrap gap-1">
                {parsed.columns.map((col) => (
                  <span
                    key={col}
                    dir="auto"
                    className="max-w-full truncate rounded-md bg-white/60 dark:bg-white/10 px-1.5 py-0.5 text-[0.6875rem] text-[#6B5B4A] dark:text-[#cfbdaa]"
                  >
                    {col}
                  </span>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}
