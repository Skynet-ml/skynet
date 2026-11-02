"use client";

import { InlineWarningRow } from "@/shared/ui/inline-warning-row";
import * as React from "react";
import Link from "next/link";
import { CircleNotch, Copy, Database, PencilSimple, Tag, Trash } from "@/shared/ui/icons";
import { toast } from "react-toastify";
import { Badge } from "@/shared/ui/primitives/badge";
import { Button } from "@/shared/ui/primitives/button";
import { Dialog, DialogContent, DialogFooter } from "@/shared/ui/primitives/dialog";
import { DialogTitleRow } from "@/shared/ui/dialog-title-row";
import { SelectCheckbox } from "@/shared/ui/select-checkbox";
import { TooltipButton } from "@/shared/ui/tooltip-button";
import {
  LIST_ROW_ACTION_DIVIDER_CLASS,
  LIST_ROW_ACTIONS_CLASS,
  LIST_ROW_CLASS,
  LIST_ROW_ICON_CLASS,
  LIST_ROW_META_CLASS,
  LIST_ROW_META_DOT_CLASS,
  LIST_ROW_SELECTED_CLASS,
  LIST_ROW_TITLE_CLASS,
} from "@/shared/ui/list-row";
import {
  cloneDataset,
  deleteDataset,
  isStorageQuotaError,
  listDatasetOptimizations,
  type DatasetSummary,
} from "@/shared/lib/api";
import { formatMsg, msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { formatBytes, formatRelativeTime } from "@/shared/lib/formatters";
import { cn } from "@/shared/lib/utils";
import { DatasetRenameDialog } from "./DatasetRenameDialog";
import { DatasetShareDialog } from "./DatasetShareDialog";

/**
 * One library dataset rendered as a clickable card: name, row/column
 * counts, size and last-updated, plus role-gated actions. Owners get share /
 * rename / delete; everyone else (shared-in) gets clone-to-my-library. Clicking
 * the card body opens the dataset page via ``onOpen``.
 */
export function DatasetCard({
  dataset,
  onOpen,
  onChanged,
  selected,
  onToggleSelect,
}: {
  dataset: DatasetSummary;
  onOpen: (dataset: DatasetSummary) => void;
  onChanged: () => void;
  selected: boolean;
  /** ``shiftKey`` is true on shift-click, extending the view's range anchor. */
  onToggleSelect: (shiftKey: boolean) => void;
}) {
  const isOwner = dataset.role === "owner";
  const [renameOpen, setRenameOpen] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);
  const [cloning, setCloning] = React.useState(false);
  // How many optimizations were built from this dataset, fetched only when the
  // delete dialog opens. ``null`` while unknown/loading; a positive count warns
  // that those runs' back-link will dangle (the runs themselves keep working —
  // they own a copy of the rows, not a reference). Owner-only, mirroring delete.
  const [usedCount, setUsedCount] = React.useState<number | null>(null);

  React.useEffect(() => {
    if (!deleteOpen || !isOwner) {
      setUsedCount(null);
      return;
    }
    let cancelled = false;
    listDatasetOptimizations(dataset.id)
      .then((res) => !cancelled && setUsedCount(res.optimizations.length))
      .catch(() => !cancelled && setUsedCount(0));
    return () => {
      cancelled = true;
    };
  }, [deleteOpen, isOwner, dataset.id]);

  const handleDelete = async () => {
    if (deleting) return;
    setDeleting(true);
    try {
      await deleteDataset(dataset.id);
      toast.success(msg("datasets.toast.deleted"));
      setDeleteOpen(false);
      onChanged();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("datasets.toast.delete_failed")));
    } finally {
      setDeleting(false);
    }
  };

  const handleClone = async () => {
    if (cloning) return;
    setCloning(true);
    try {
      const res = await cloneDataset(dataset.id);
      toast.success(
        res.deduplicated ? msg("datasets.toast.deduplicated") : msg("datasets.toast.cloned"),
      );
      onChanged();
    } catch (err) {
      if (!isStorageQuotaError(err)) {
        toast.error(userErrorMessage(err, msg("datasets.toast.clone_failed")));
      }
    } finally {
      setCloning(false);
    }
  };

  // Buttons inside the clickable card stop propagation so their own handlers
  // fire without also opening the detail sheet.
  const stop = (e: React.MouseEvent) => e.stopPropagation();

  return (
    <>
      <div
        role="button"
        tabIndex={0}
        onClick={() => onOpen(dataset)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onOpen(dataset);
          }
        }}
        className={cn(LIST_ROW_CLASS, selected && LIST_ROW_SELECTED_CLASS)}
      >
        {/* Shared-in datasets can't be bulk-deleted, so their checkbox is an
            invisible placeholder that keeps the rows column-aligned. */}
        <span className={cn("flex shrink-0 items-center", !isOwner && "invisible")}>
          <SelectCheckbox
            checked={selected}
            onToggle={onToggleSelect}
            disabled={!isOwner}
            ariaLabel={formatMsg("shared.selection.select_named", { name: dataset.name })}
          />
        </span>
        <span className={LIST_ROW_ICON_CLASS}>
          <Database className="size-4" />
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className={LIST_ROW_TITLE_CLASS} dir="auto">
              {dataset.name}
            </p>
            {!isOwner && (
              <Badge variant="secondary" size="sm">
                {msg("datasets.shared_badge")}
              </Badge>
            )}
          </div>
          <p className={LIST_ROW_META_CLASS}>
            {[
              formatMsg("datasets.count.rows", { count: dataset.row_count }),
              formatMsg("datasets.count.columns", { count: dataset.column_count }),
              formatBytes(dataset.byte_size),
              formatRelativeTime(dataset.updated_at),
            ].map((part, i) => (
              <React.Fragment key={i}>
                {i > 0 && (
                  <span aria-hidden="true" className={LIST_ROW_META_DOT_CLASS}>
                    ·
                  </span>
                )}
                <span className="shrink-0 last:min-w-0 last:shrink last:truncate">{part}</span>
              </React.Fragment>
            ))}
          </p>
        </div>

        <div className={LIST_ROW_ACTIONS_CLASS} onClick={stop}>
          <TooltipButton tooltip={msg("datasets.action.tag")}>
            <Button
              asChild
              variant="ghost"
              size="icon-sm"
              className="text-muted-foreground hover:text-foreground"
              aria-label={msg("datasets.action.tag")}
            >
              <Link href={`/tagger?dataset=${dataset.id}&name=${encodeURIComponent(dataset.name)}`}>
                <Tag className="size-4" />
              </Link>
            </Button>
          </TooltipButton>
          {isOwner ? (
            <>
              <DatasetShareDialog datasetId={dataset.id} />
              <TooltipButton tooltip={msg("datasets.action.rename")}>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="text-muted-foreground hover:text-foreground"
                  onClick={() => setRenameOpen(true)}
                  aria-label={msg("datasets.action.rename")}
                >
                  <PencilSimple className="size-4" />
                </Button>
              </TooltipButton>
              <span aria-hidden="true" className={LIST_ROW_ACTION_DIVIDER_CLASS} />
              <Button
                variant="ghost"
                size="icon-sm"
                className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                onClick={() => setDeleteOpen(true)}
                autoTooltip={false}
                aria-label={msg("datasets.action.delete")}
              >
                <Trash className="size-4" />
              </Button>
            </>
          ) : (
            <TooltipButton tooltip={msg("datasets.action.clone")}>
              <Button
                variant="ghost"
                size="icon-sm"
                className="text-muted-foreground hover:text-foreground"
                onClick={handleClone}
                disabled={cloning}
                aria-label={msg("datasets.action.clone")}
              >
                {cloning ? (
                  <CircleNotch
                    className="animate-spin motion-reduce:animate-none"
                    aria-hidden="true"
                  />
                ) : (
                  <Copy className="size-4" />
                )}
              </Button>
            </TooltipButton>
          )}
        </div>
      </div>

      <DatasetRenameDialog
        dataset={dataset}
        open={renameOpen}
        onOpenChange={setRenameOpen}
        onRenamed={onChanged}
      />

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent className="w-[min(28rem,92vw)] max-w-[min(28rem,92vw)] sm:max-w-md">
          <DialogTitleRow
            title={msg("datasets.delete.title")}
            description={msg("datasets.delete.body")}
          />
          {usedCount !== null &&
            usedCount > 0 &&
            (() => {
              // Bold the affected-run count to match how every other delete dialog
              // emphasizes its key value.
              const [warnBefore, warnAfter = ""] = msg("datasets.delete.used_warning").split(
                "{count}",
              );
              return (
                <InlineWarningRow
                  message={
                    <>
                      {warnBefore}
                      <span className="font-semibold">{usedCount}</span>
                      {warnAfter}
                    </>
                  }
                />
              );
            })()}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)} disabled={deleting}>
              {msg("datasets.delete.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleDelete} disabled={deleting}>
              {deleting ? (
                <CircleNotch
                  className="animate-spin motion-reduce:animate-none"
                  aria-hidden="true"
                />
              ) : (
                msg("datasets.delete.confirm")
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
