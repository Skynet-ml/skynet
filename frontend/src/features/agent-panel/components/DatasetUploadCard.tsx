"use client";

import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { Button } from "@/shared/ui/primitives/button";
import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Check, CircleNotch, FileXls, UploadSimple } from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { DATASET_UPLOAD_ACCEPT, parseDatasetFile } from "@/shared/lib/parse-dataset";

import type { AgentToolCall } from "@/shared/ui/agent/types";

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
  onConfirm: (confirmed: ConfirmedDataset) => void;
}

const DATASET_FILE_ACCEPT = DATASET_UPLOAD_ACCEPT;

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

  React.useEffect(() => {
    if (alreadyConfirmed) setConfirmed(true);
  }, [alreadyConfirmed]);

  // Each read gets a number; a newer read, an unmount or a Strict Mode
  // remount drops an older one, so a closed card attaches nothing.
  const readRef = React.useRef(0);

  const handleFile = React.useCallback(async (file: File) => {
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
      onConfirmRef.current(read);
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
          "relative overflow-hidden rounded-2xl border bg-card",
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
                  "group flex h-[164px] w-full flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed px-4",
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
