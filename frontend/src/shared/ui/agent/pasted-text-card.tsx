"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { FileText, X } from "@/shared/ui/icons";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/primitives/button";
import { Dialog, DialogContent, DialogFooter, DialogTitle } from "@/shared/ui/primitives/dialog";

import { countLines } from "./pasted-text";

const MessageMarkdown = dynamic(() => import("./message-markdown").then((m) => m.MessageMarkdown), {
  ssr: false,
});

interface PastedTextCardProps {
  text: string;
  /** An attached file's name; without one the card is a long paste. */
  name?: string;
  /** Composer only: drop the paste from the draft. */
  onRemove?: () => void;
  /** Composer only: put the paste back into the text field as plain draft. */
  onRestore?: () => void;
  className?: string;
}

/**
 * A long paste shown as a file card, the way ChatGPT shows one: a document
 * tile, a name and a line count. Clicking it opens the full text.
 */
export function PastedTextCard({
  text,
  name,
  onRemove,
  onRestore,
  className,
}: PastedTextCardProps) {
  const [open, setOpen] = React.useState(false);
  const title = name ?? msg("shared.agent.pasted_text");
  const lines = msg("shared.code_editor.lines_count", { count: countLines(text) });

  return (
    <>
      <div className={cn("group/paste relative w-56 max-w-full", className)}>
        <button
          type="button"
          onClick={() => setOpen(true)}
          aria-label={msg("shared.agent.pasted_text_open")}
          className="flex w-full items-center gap-2.5 rounded-xl border border-border bg-card p-2 text-start transition-colors hover:bg-muted/60 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
        >
          <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary-fill text-primary-foreground">
            <FileText className="size-4.5" />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium text-foreground" dir="auto">
              {title}
            </span>
            <span className="block truncate text-xs text-muted-foreground">{lines}</span>
          </span>
        </button>
        {onRemove && (
          <button
            type="button"
            onClick={onRemove}
            aria-label={msg("shared.agent.pasted_text_remove")}
            className="absolute -top-1.5 -end-1.5 flex size-5 items-center justify-center rounded-full border border-border bg-background text-muted-foreground shadow-sm transition-opacity hover:text-foreground cursor-pointer opacity-0 group-hover/paste:opacity-100 focus-visible:opacity-100 any-pointer-coarse:opacity-100"
          >
            <X className="size-3" />
          </button>
        )}
      </div>
      <TextPreviewDialog
        open={open}
        onOpenChange={setOpen}
        title={title}
        text={text}
        onRestore={onRestore}
      />
    </>
  );
}

/** The full text of a paste or an attached file, with its name and line count. */
export function TextPreviewDialog({
  open,
  onOpenChange,
  title,
  text,
  onRestore,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  text: string;
  /** Composer only: put the text back into the text field as plain draft. */
  onRestore?: () => void;
}) {
  const lines = msg("shared.code_editor.lines_count", { count: countLines(text) });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[85dvh] flex-col overflow-hidden sm:max-w-[min(48rem,92vw)]">
        <DialogTitle className="flex items-baseline gap-2">
          <span className="min-w-0 truncate" dir="auto">
            {title}
          </span>
          <span className="text-xs font-normal text-muted-foreground">{lines}</span>
        </DialogTitle>
        <div className="min-h-0 flex-1 overflow-y-auto text-sm" dir="auto">
          <React.Suspense fallback={<pre className="whitespace-pre-wrap">{text}</pre>}>
            <MessageMarkdown content={text} />
          </React.Suspense>
        </div>
        {onRestore && (
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                onOpenChange(false);
                onRestore();
              }}
            >
              {msg("shared.agent.pasted_text_restore")}
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}
