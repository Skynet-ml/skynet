"use client";

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  Database,
  MagnifyingGlass,
  PencilSimple,
  Table,
  Tag,
  UploadSimple,
  Users,
} from "@/shared/ui/icons";
import { toast } from "react-toastify";
import { Button } from "@/shared/ui/primitives/button";
import { DataHubTabs } from "@/shared/ui/data-hub-tabs";
import { EmptyState } from "@/shared/ui/empty-state";
import { SearchField } from "@/shared/ui/search-field";
import { SelectionAction, SelectionBar } from "@/shared/ui/selection-bar";
import {
  bulkDeleteDatasets,
  isStorageQuotaError,
  saveDataset,
  type ConnectorProvider,
  type DatasetSummary,
} from "@/shared/lib/api";
import { formatMsg, msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { DATASET_UPLOAD_ACCEPT, parseDatasetFile } from "@/shared/lib/parse-dataset";
import { track, TelemetryEvent } from "@/shared/lib/telemetry";
import { cn } from "@/shared/lib/utils";
import { ImportFromMenu } from "@/features/connectors";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { useDatasets } from "../hooks/use-datasets";
import { DatasetCard } from "./DatasetCard";
import { DatasetRenameDialog } from "./DatasetRenameDialog";
import { DatasetShareDialog } from "./DatasetShareDialog";
import { DATASETS_LAYOUT_KEY, DatasetsSkeleton } from "./DatasetsSkeleton";
import { rememberLayout } from "@/shared/lib/layout-hint";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

/**
 * Top-level /datasets page: the personal dataset library. Lists owned and
 * shared-in datasets as searchable cards over a usage meter, with a drag-in /
 * click upload that parses CSV/JSON/XLSX and saves a new entry. Clicking a card
 * (or arriving with ``?open=<id>``) opens that dataset's page, where it can be
 * viewed, edited, exported, tagged and traced to the optimizations that used it.
 */
export function DatasetsView() {
  const { datasets, loading, error, refetch } = useDatasets();
  const handleImported = (dataset: DatasetSummary, provider: ConnectorProvider) => {
    track(TelemetryEvent.DatasetCreated, { source: provider, rows: dataset.row_count });
    refetch();
  };
  const searchParams = useSearchParams();
  const router = useRouter();
  const [search, setSearch] = React.useState("");
  const openDataset = React.useCallback(
    (dataset: DatasetSummary, replace = false) => {
      const href = `/datasets/${dataset.id}/edit?name=${encodeURIComponent(dataset.name)}`;
      if (replace) router.replace(href);
      else router.push(href);
    },
    [router],
  );
  const [dragging, setDragging] = React.useState(false);
  const [uploading, setUploading] = React.useState(false);
  const [selectedIds, setSelectedIds] = React.useState<Set<string>>(new Set());
  // Last-toggled dataset id — the shift-click range anchor, kept as an id (not
  // an index) so it survives the search filter reordering the visible list.
  const [anchorId, setAnchorId] = React.useState<string | null>(null);
  const [bulkOpen, setBulkOpen] = React.useState(false);
  const [bulkDeleting, setBulkDeleting] = React.useState(false);
  const [shareOpen, setShareOpen] = React.useState(false);
  const [renameOpen, setRenameOpen] = React.useState(false);
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const deepLinkedRef = React.useRef(false);

  // Drop selections that stopped resolving to an owned dataset (deleted in
  // another tab, or ownership changed), so the bar never counts ghosts.
  React.useEffect(() => {
    setSelectedIds((prev) => {
      const next = new Set(
        [...prev].filter((id) => datasets.some((d) => d.id === id && d.role === "owner")),
      );
      return next.size === prev.size ? prev : next;
    });
  }, [datasets]);

  const confirmBulkDelete = async () => {
    if (bulkDeleting) return;
    setBulkDeleting(true);
    try {
      const res = await bulkDeleteDatasets([...selectedIds]);
      if (res.deleted.length > 0) {
        toast.success(formatMsg("datasets.toast.bulk_deleted", { count: res.deleted.length }));
      }
      if (res.skipped.length > 0) {
        toast.warning(formatMsg("shared.selection.delete_skipped", { count: res.skipped.length }));
      }
      setBulkOpen(false);
      setSelectedIds(new Set());
      setAnchorId(null);
      refetch();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("datasets.toast.delete_failed")));
    } finally {
      setBulkDeleting(false);
    }
  };

  // Honour ?open=<id> once the list has loaded: go to that dataset's page (the
  // navigable link from an optimization's source-dataset row). Guarded so it
  // fires a single time. When the id resolves to nothing — the source dataset
  // was deleted or unshared — say so rather than dead-ending silently.
  React.useEffect(() => {
    if (deepLinkedRef.current || loading || error) return;
    const openId = searchParams.get("open");
    if (!openId) return;
    deepLinkedRef.current = true;
    const match = datasets.find((d) => d.id === openId);
    if (match) openDataset(match, true);
    else toast.info(msg("datasets.open.not_found"));
  }, [datasets, loading, error, searchParams, openDataset]);

  // Per-dataset actions only make sense with exactly one selected, as in the
  // dashboard jobs bar; selection is owner-only, so every one of them applies.
  const soleSelected =
    selectedIds.size === 1 ? (datasets.find((d) => selectedIds.has(d.id)) ?? null) : null;

  const filtered = React.useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return datasets;
    return datasets.filter((d) => d.name.toLowerCase().includes(q));
  }, [datasets, search]);

  // Same mechanism as the storage cleanup drawer: plain click toggles one row,
  // shift-click applies the clicked row's new state to the whole visible range
  // between it and the previous toggle. Shared-in rows inside a range are
  // skipped — only owned datasets can be bulk-deleted.
  const toggleSelected = React.useCallback(
    (id: string, shiftKey: boolean) => {
      const visible = filtered;
      setSelectedIds((prev) => {
        const next = new Set(prev);
        const willSelect = !prev.has(id);
        const index = visible.findIndex((d) => d.id === id);
        const anchor = anchorId === null ? -1 : visible.findIndex((d) => d.id === anchorId);
        if (shiftKey && anchor !== -1 && index !== -1) {
          const [lo, hi] = anchor < index ? [anchor, index] : [index, anchor];
          for (let i = lo; i <= hi; i++) {
            const row = visible[i];
            if (!row || row.role !== "owner") continue;
            if (willSelect) next.add(row.id);
            else next.delete(row.id);
          }
        } else if (willSelect) {
          next.add(id);
        } else {
          next.delete(id);
        }
        return next;
      });
      setAnchorId(id);
    },
    [filtered, anchorId],
  );

  const handleFiles = React.useCallback(
    async (files: FileList | null) => {
      const file = files?.[0];
      if (!file || uploading) return;
      setUploading(true);
      try {
        const parsed = await parseDatasetFile(file);
        const name = file.name.replace(/\.[^.]+$/, "") || file.name;
        await saveDataset({
          name,
          source: "upload",
          dataset: parsed.rows,
          column_schema: { column_order: parsed.columns },
        });
        track(TelemetryEvent.DatasetCreated, { source: "upload", rows: parsed.rows.length });
        toast.success(msg("datasets.toast.uploaded"));
        refetch();
      } catch (err) {
        // The storage-budget 409 opens the shared quota modal centrally, so
        // suppress the redundant toast here (and at the other save producers).
        if (!isStorageQuotaError(err)) {
          toast.error(userErrorMessage(err, msg("datasets.toast.upload_failed")));
        }
      } finally {
        setUploading(false);
      }
    },
    [uploading, refetch],
  );

  const datasetCount = loading || error ? null : datasets.length;
  React.useEffect(() => {
    if (datasetCount != null) rememberLayout(DATASETS_LAYOUT_KEY, datasetCount);
  }, [datasetCount]);

  if (loading) {
    return (
      <div className="pb-16">
        <DataHubTabs active="datasets" />
        <DatasetsSkeleton />
      </div>
    );
  }

  return (
    <div className="pb-16">
      <DataHubTabs active="datasets" />
      <input
        ref={fileInputRef}
        type="file"
        accept={DATASET_UPLOAD_ACCEPT}
        className="hidden"
        onChange={(e) => {
          void handleFiles(e.target.files);
          e.target.value = "";
        }}
      />

      {datasets.length > 0 && (
        <div className="flex flex-col gap-2.5 sm:flex-row sm:items-center">
          <SearchField
            value={search}
            onValueChange={setSearch}
            placeholder={msg("datasets.search.placeholder")}
            className="flex-1"
          />
          <ImportFromMenu
            onImported={handleImported}
            data-telemetry="datasets-connector-import"
            className="!h-[44px] w-full shrink-0 rounded-2xl sm:w-auto"
          />
          <Button
            variant="outline"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            data-telemetry="datasets-upload"
            className="!h-[44px] w-full shrink-0 rounded-2xl sm:w-auto"
          >
            <UploadSimple className="size-4" />
            {msg("datasets.upload")}
          </Button>
        </div>
      )}

      <div
        onDragOver={(e) => {
          e.preventDefault();
          if (!dragging) setDragging(true);
        }}
        onDragLeave={(e) => {
          e.preventDefault();
          if (e.currentTarget === e.target) setDragging(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          void handleFiles(e.dataTransfer.files);
        }}
        className={cn(
          "mt-5 rounded-xl border border-dashed transition-colors duration-150",
          dragging ? "border-primary-fill/50 bg-primary-fill/[0.03]" : "border-transparent",
        )}
      >
        {error ? (
          <EmptyState icon={Database} title={msg("datasets.error")} />
        ) : datasets.length === 0 ? (
          <EmptyState
            icon={Database}
            iconWrap="tile"
            title={msg("datasets.empty.title")}
            description={msg("datasets.empty.body")}
            action={{ label: msg("datasets.upload"), onClick: () => fileInputRef.current?.click() }}
          >
            <ImportFromMenu
              variant="ghost"
              size="sm"
              onImported={handleImported}
              data-telemetry="datasets-connector-import"
            />
          </EmptyState>
        ) : filtered.length === 0 ? (
          <EmptyState icon={MagnifyingGlass} title={msg("datasets.search.empty")} />
        ) : (
          <div className="flex flex-col gap-2.5 p-0.5">
            {dragging && (
              <p className="pointer-events-none py-2 text-center text-sm font-medium text-primary/70">
                {msg("datasets.upload.drop")}
              </p>
            )}
            {filtered.map((dataset) => (
              <DatasetCard
                key={dataset.id}
                dataset={dataset}
                onOpen={openDataset}
                onChanged={refetch}
                selected={selectedIds.has(dataset.id)}
                onToggleSelect={(shiftKey) => toggleSelected(dataset.id, shiftKey)}
              />
            ))}
          </div>
        )}
      </div>

      <SelectionBar
        count={selectedIds.size}
        onClear={() => {
          setSelectedIds(new Set());
          setAnchorId(null);
        }}
        onDelete={() => setBulkOpen(true)}
      >
        {soleSelected && (
          <>
            <SelectionAction
              label={msg("datasets.action.tag")}
              onClick={() =>
                router.push(
                  `/tagger?dataset=${soleSelected.id}&name=${encodeURIComponent(soleSelected.name)}`,
                )
              }
            >
              <Tag className="size-4" />
            </SelectionAction>
            <SelectionAction
              label={msg("datasets.action.edit")}
              onClick={() =>
                router.push(
                  `/datasets/${soleSelected.id}/edit?name=${encodeURIComponent(soleSelected.name)}`,
                )
              }
            >
              <Table className="size-4" />
            </SelectionAction>
            {!isLocalMode() && (
              <SelectionAction label={msg("share.button")} onClick={() => setShareOpen(true)}>
                <Users className="size-4" />
              </SelectionAction>
            )}
            <SelectionAction
              label={msg("datasets.action.rename")}
              onClick={() => setRenameOpen(true)}
            >
              <PencilSimple className="size-4" />
            </SelectionAction>
          </>
        )}
      </SelectionBar>

      {soleSelected && (
        <>
          <DatasetShareDialog
            key={soleSelected.id}
            datasetId={soleSelected.id}
            open={shareOpen}
            onOpenChange={setShareOpen}
            hideTrigger
          />
          <DatasetRenameDialog
            dataset={soleSelected}
            open={renameOpen}
            onOpenChange={setRenameOpen}
            onRenamed={refetch}
          />
        </>
      )}

      <ConfirmDialog
        open={bulkOpen}
        onOpenChange={setBulkOpen}
        variant="danger"
        title={msg("datasets.delete.selected_title")}
        description={formatMsg("datasets.delete.selected_body", { count: selectedIds.size })}
        cancelLabel={msg("datasets.delete.cancel")}
        confirmLabel={msg("datasets.delete.confirm")}
        onConfirm={confirmBulkDelete}
        pending={bulkDeleting}
      />
    </div>
  );
}
