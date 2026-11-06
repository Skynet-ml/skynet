"use client";

import { clipText } from "@/shared/lib/clip-text";
import { notifyCopied } from "@/shared/lib/notify";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { ProgressBar } from "@/shared/ui/progress-bar";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTableSort } from "@/shared/hooks/use-table-sort";
import { toast } from "react-toastify";
import { CircleNotch, ClockCounterClockwise, MagicWand, Tray } from "@/shared/ui/icons";
import { Card, CardContent } from "@/shared/ui/primitives/card";
import { EmptyState } from "@/shared/ui/empty-state";
import { Table, TableBody, TableCell, TableHeader, TableRow } from "@/shared/ui/primitives/table";
import {
  ColumnHeader,
  useColumnFilters,
  useColumnResize,
  ResetColumnsButton,
  ResetFiltersButton,
} from "@/shared/ui/excel-filter";
import { DataTabSkeleton, dataTabLayoutKey, type DataTabShape } from "./DataTabSkeleton";
import { rememberLayout } from "@/shared/lib/layout-hint";
import { ExportTableMenu } from "@/shared/ui/export-table-menu";
import { FadeIn } from "@/shared/ui/motion";
import { Segmented } from "@/shared/ui/segmented";
import { HelpTip } from "@/shared/ui/help-tip";
import { msg } from "@/shared/lib/messages";
import { DatasetRowReader } from "@/features/datasets";
import { tip } from "@/shared/lib/tooltips";
import { getOptimizationDataset, getTestResults, getPairTestResults } from "@/shared/lib/api";
import type { ApiSource } from "@/shared/lib/api-route";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import type {
  OptimizationDatasetResponse,
  OptimizationStatusResponse,
  EvalExampleResult,
} from "@/shared/types/api";

type Split = "all" | "train" | "val" | "test";
type ProgramType = "optimized" | "baseline";

/**
 * Map a score (0–1) onto a single-hue ramp from muted ink to the theme accent.
 *
 * One hue keeps the table calm: strong scores read as accent, weak ones fade
 * back, instead of a red → amber → green traffic light on every row.
 */
function scoreColor(score: number): string {
  const pct = Math.round(Math.max(0, Math.min(1, score)) * 100);
  return `color-mix(in oklch, var(--primary) ${pct}%, var(--muted-foreground))`;
}

/**
 * Render a dataset cell value as a string the table can display.
 *
 * React datasets carry structured columns — `chat_history` is an array of
 * turn objects, `wizard_state` an object — that `String(value)` collapses to
 * the useless `[object Object]`. Stringify those to JSON so the cell shows the
 * real content (compact inline, indented for the hover title); scalars pass
 * through unchanged. `null`/`undefined` render as an empty string.
 */
function formatCellValue(value: unknown, pretty = false): string {
  if (value == null) return "";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, pretty ? 2 : undefined);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

export function DataTab({
  job,
  pairIndex,
  sharedDataset,
  sharedTestResults,
  source,
}: {
  job: OptimizationStatusResponse;
  /** Where the run lives; a hosted run reads through the hosted proxy. */
  source?: ApiSource;
  pairIndex?: number | null;
  /** Public share view: render this injected split instead of fetching (no per-example scores). */
  sharedDataset?: OptimizationDatasetResponse | null;
  /** Public share view: inject baseline/optimized eval results instead of fetching them. */
  sharedTestResults?: { baseline: EvalExampleResult[]; optimized: EvalExampleResult[] } | null;
}) {
  const [dataset, setDataset] = useState<OptimizationDatasetResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [split, setSplit] = useState<Split>("test");
  const [programType, setProgramType] = useState<ProgramType>("optimized");
  const [testResults, setTestResults] = useState<Record<string, Record<number, EvalExampleResult>>>(
    { optimized: {}, baseline: {} },
  );
  const [testResultsLoading, setTestResultsLoading] = useState(false);

  const colFilters = useColumnFilters();
  const { sortKey, sortDir, toggleSort } = useTableSort<string>("");
  const colResize = useColumnResize();

  useEffect(() => {
    if (sharedDataset) {
      setDataset(sharedDataset);
      setLoading(false);
      return;
    }
    getOptimizationDataset(job.optimization_id, source)
      .then(setDataset)
      .catch(() => setError(msg("auto.features.optimizations.components.datatab.literal.1")))
      .finally(() => setLoading(false));
  }, [job.optimization_id, sharedDataset, source]);

  // Share view: index the injected baseline/optimized results by example index
  // (mirrors the fetch path's reshaping) instead of calling the authed endpoint.
  useEffect(() => {
    if (!sharedTestResults) return;
    const optimized: Record<number, EvalExampleResult> = {};
    const baseline: Record<number, EvalExampleResult> = {};
    for (const r of sharedTestResults.optimized ?? []) optimized[r.index] = r;
    for (const r of sharedTestResults.baseline ?? []) baseline[r.index] = r;
    setTestResults({ optimized, baseline });
  }, [sharedTestResults]);

  useEffect(() => {
    if (sharedDataset || job.status !== "success") return;
    setTestResultsLoading(true);
    const fetchResults =
      pairIndex != null
        ? getPairTestResults(job.optimization_id, pairIndex, source)
        : getTestResults(job.optimization_id, source);
    fetchResults
      .then((res) => {
        const optimized: Record<number, EvalExampleResult> = {};
        const baseline: Record<number, EvalExampleResult> = {};
        for (const r of res.optimized ?? []) optimized[r.index] = r;
        for (const r of res.baseline ?? []) baseline[r.index] = r;
        setTestResults({ optimized, baseline });
      })
      .catch((err) => {
        // Test-results endpoint is non-critical for the data view; log so the
        // failure is visible in dev tools without breaking the dataset render.
        console.warn("test results fetch failed:", err);
      })
      .finally(() => setTestResultsLoading(false));
  }, [job.optimization_id, job.status, pairIndex, sharedDataset, source]);

  const inputFields = useMemo(
    () => (dataset ? Object.values(dataset.column_mapping.inputs) : []),
    [dataset],
  );
  const outputFields = useMemo(
    () => (dataset ? Object.values(dataset.column_mapping.outputs) : []),
    [dataset],
  );
  const allColumns = useMemo(() => [...inputFields, ...outputFields], [inputFields, outputFields]);

  const currentResults = testResults[programType] ?? {};

  // Named scores the metric logged per row via log_metrics — one column per
  // name after the prediction columns. Union across rows so a name logged on
  // only some rows still gets a column; capped so a metric that (against the
  // contract) mints per-example names can't explode the table.
  const loggedMetricNames = useMemo(() => {
    const names = new Set<string>();
    for (const result of Object.values(currentResults)) {
      for (const name of Object.keys(result.logged_metrics ?? {})) names.add(name);
    }
    return [...names].slice(0, 30);
  }, [currentResults]);

  const rows = useMemo(() => {
    if (!dataset) return [];
    if (split === "all")
      return [...dataset.splits.train, ...dataset.splits.val, ...dataset.splits.test];
    return dataset.splits[split];
  }, [dataset, split]);

  const filtered = useMemo(() => {
    let result = rows.filter((r) => {
      for (const [col, allowed] of Object.entries(colFilters.filters)) {
        if (allowed.size === 0) continue;
        const val = formatCellValue(r.row[col]);
        if (!allowed.has(val)) return false;
      }
      return true;
    });
    if (sortKey) {
      const collLocale = getActiveIntlLocale();
      result = [...result].sort((a, b) => {
        const av = formatCellValue(a.row[sortKey]);
        const bv = formatCellValue(b.row[sortKey]);
        const cmp = av.localeCompare(bv, collLocale, { numeric: true });
        return sortDir === "asc" ? cmp : -cmp;
      });
    }
    return result;
  }, [rows, colFilters.filters, sortKey, sortDir]);

  // The export and the row reader show the same flattened record. The reader
  // passes the one row it shows, since flattening every row on each step stalled it.
  const buildRecords = (only?: number) => {
    const includeEval = split === "test" && evalCount > 0;
    const scoreLabel = msg("auto.features.optimizations.components.datatab.literal.8");
    const columns: string[] = [
      ...(includeEval ? [scoreLabel] : []),
      ...inputFields,
      ...outputFields,
      ...(includeEval ? outputFields.map((f) => `pred_${f}`) : []),
      ...(includeEval ? loggedMetricNames : []),
    ];
    const source = only === undefined ? filtered : filtered.slice(only, only + 1);
    const rows = source.map((row) => {
      const ev = currentResults[row.index];
      const rec: Record<string, unknown> = {};
      if (includeEval) rec[scoreLabel] = ev ? ev.score : null;
      for (const f of inputFields) rec[f] = formatCellValue(row.row[f]);
      for (const f of outputFields) rec[f] = formatCellValue(row.row[f]);
      if (includeEval) {
        for (const f of outputFields) {
          const sigField = Object.entries(dataset?.column_mapping.outputs ?? {}).find(
            ([, col]) => col === f,
          )?.[0];
          rec[`pred_${f}`] = formatCellValue(ev?.outputs[sigField ?? ""]);
        }
        for (const name of loggedMetricNames) {
          const value = ev?.logged_metrics?.[name];
          rec[name] = value != null ? Number(value.toFixed(3)) : null;
        }
      }
      return rec;
    });
    return { columns, rows };
  };

  // Index into ``filtered``; non-null swaps the table for the row reader.
  const [readerIndex, setReaderIndex] = useState<number | null>(null);
  useEffect(() => setReaderIndex(null), [filtered]);
  const reader = readerIndex === null ? null : buildRecords(readerIndex);

  // A single click copies the cell, but a double-click opens the reader, so
  // the copy waits long enough to know no second click is coming.
  const pendingCopy = useRef<number | null>(null);
  const cancelPendingCopy = useCallback(() => {
    if (pendingCopy.current !== null) window.clearTimeout(pendingCopy.current);
    pendingCopy.current = null;
  }, []);
  useEffect(() => cancelPendingCopy, [cancelPendingCopy]);
  const openReader = (index: number) => {
    cancelPendingCopy();
    setReaderIndex(index);
  };

  // Only the open dropdown reads its options, so build just that column's
  // list instead of deduping and sorting every column on each load.
  const openFilterCol = colFilters.openFilter;
  const filterOptions = useMemo(() => {
    const opts: Record<string, Array<{ value: string; label: string }>> = {};
    if (openFilterCol && allColumns.includes(openFilterCol)) {
      const vals = [...new Set(rows.map((r) => formatCellValue(r.row[openFilterCol])))]
        .filter(Boolean)
        .sort();
      opts[openFilterCol] = vals.map((v) => ({
        value: v,
        label: v.length > 40 ? `${v.slice(0, 40)}...` : v,
      }));
    }
    return opts;
  }, [rows, allColumns, openFilterCol]);

  const evalCount = Object.keys(currentResults).length;

  const scoreColumns = split === "test" && evalCount > 0;
  const columnCount =
    inputFields.length +
    outputFields.length +
    (scoreColumns ? 1 + outputFields.length + loggedMetricNames.length : 0);
  const tableRows = Math.min(filtered.length, 200);
  useEffect(() => {
    if (loading || error || sharedDataset) return;
    rememberLayout(dataTabLayoutKey(job.optimization_id), {
      columns: columnCount,
      rows: tableRows,
      score: scoreColumns,
    } satisfies DataTabShape);
  }, [loading, error, sharedDataset, job.optimization_id, columnCount, tableRows, scoreColumns]);

  if (loading) return <DataTabSkeleton optimizationId={job.optimization_id} />;
  if (error || !dataset)
    return (
      <InlineErrorRow
        message={error ?? msg("auto.features.optimizations.components.datatab.literal.2")}
      />
    );

  return (
    <div className="space-y-4 mt-4">
      <FadeIn>
        <p className="text-sm text-muted-foreground">{msg("optimizations.datatab.description")}</p>
      </FadeIn>
      {/* Test evaluation bar — shows cached results */}
      {split === "test" && (
        <FadeIn delay={0.2}>
          <div className="rounded-2xl border border-border-subtle bg-muted/40 p-4 space-y-3">
            <div className="flex items-center gap-3">
              <div className="flex-1 min-w-0">
                <div className="text-sm font-semibold text-foreground">
                  <HelpTip text={tip("code.predictions_table")}>
                    {msg("auto.features.optimizations.components.datatab.1")}
                  </HelpTip>
                </div>
              </div>
              <Segmented<ProgramType>
                size="sm"
                iconOnly
                className="shrink-0"
                segmentClassName="min-w-[44px] px-0 lg:min-h-8 lg:min-w-8"
                value={programType}
                onChange={setProgramType}
                options={[
                  {
                    value: "baseline",
                    label: msg("auto.features.optimizations.components.datatab.2"),
                    tip: msg("auto.features.optimizations.components.datatab.2"),
                    icon: <ClockCounterClockwise className="size-3.5" aria-hidden="true" />,
                  },
                  {
                    value: "optimized",
                    label: msg("auto.features.optimizations.components.datatab.3"),
                    tip: msg("auto.features.optimizations.components.datatab.3"),
                    icon: <MagicWand className="size-3.5" aria-hidden="true" />,
                  },
                ]}
              />
              {testResultsLoading && (
                <CircleNotch className="size-4 animate-spin text-muted-foreground shrink-0" />
              )}
            </div>
          </div>
        </FadeIn>
      )}

      <FadeIn delay={0.3}>
        <div className="flex items-center gap-3 flex-wrap">
          <div className="w-full">
            <Segmented<Split>
              size="sm"
              className="w-full"
              value={split}
              onChange={setSplit}
              options={[
                {
                  value: "all",
                  label: msg("auto.features.optimizations.components.datatab.literal.4"),
                },
                {
                  value: "train",
                  label: msg("auto.features.optimizations.components.datatab.literal.5"),
                },
                {
                  value: "val",
                  label: msg("auto.features.optimizations.components.datatab.literal.6"),
                },
                {
                  value: "test",
                  label: msg("auto.features.optimizations.components.datatab.literal.7"),
                },
              ]}
            />
          </div>
          <ResetFiltersButton filters={colFilters} />
          <ResetColumnsButton resize={colResize} />
          <div className="text-[0.625rem] text-muted-foreground tabular-nums me-auto">
            {filtered.length}
            {msg("auto.features.optimizations.components.datatab.4")}
          </div>
          <ExportTableMenu
            iconOnly
            disabled={filtered.length === 0}
            getData={() => ({ ...buildRecords(), filename: `dataset_${split}` })}
          />
        </div>
      </FadeIn>

      <FadeIn delay={0.35}>
        {filtered.length === 0 ? (
          <EmptyState
            variant="list"
            icon={Tray}
            title={msg("auto.features.optimizations.components.datatab.5")}
          />
        ) : (
          <Card
            onKeyDown={(e) => {
              if (e.key === "Escape" && readerIndex !== null) setReaderIndex(null);
            }}
          >
            <CardContent className="p-0">
              {reader && readerIndex !== null && reader.rows[0] ? (
                <div className="flex h-[520px] flex-col">
                  <DatasetRowReader
                    columns={reader.columns}
                    row={reader.rows[0]}
                    index={readerIndex}
                    total={filtered.length}
                    onStep={(delta) =>
                      setReaderIndex((cur) =>
                        cur === null || cur + delta < 0 || cur + delta >= filtered.length
                          ? cur
                          : cur + delta,
                      )
                    }
                    onClose={() => setReaderIndex(null)}
                  />
                </div>
              ) : (
                <div className="table-scroll max-h-[520px] overflow-y-auto">
                  <Table className="table-fixed">
                    <TableHeader>
                      <TableRow>
                        {split === "test" && evalCount > 0 && (
                          <ColumnHeader
                            label={msg("auto.features.optimizations.components.datatab.literal.8")}
                            sortKey="_score"
                            currentSort={sortKey}
                            sortDir={sortDir}
                            onSort={toggleSort}
                            width={colResize.widths["_score"]}
                            onResize={colResize.setColumnWidth}
                          />
                        )}
                        {inputFields.map((f) => (
                          <ColumnHeader
                            key={f}
                            label={f}
                            sortKey={f}
                            currentSort={sortKey}
                            sortDir={sortDir}
                            onSort={toggleSort}
                            filterCol={f}
                            filterOptions={filterOptions[f] ?? []}
                            filters={colFilters.filters}
                            onFilter={colFilters.setColumnFilter}
                            openFilter={colFilters.openFilter}
                            setOpenFilter={colFilters.setOpenFilter}
                            width={colResize.widths[f]}
                            onResize={colResize.setColumnWidth}
                          />
                        ))}
                        {outputFields.map((f) => (
                          <ColumnHeader
                            key={f}
                            label={f}
                            sortKey={f}
                            currentSort={sortKey}
                            sortDir={sortDir}
                            onSort={toggleSort}
                            filterCol={f}
                            filterOptions={filterOptions[f] ?? []}
                            filters={colFilters.filters}
                            onFilter={colFilters.setColumnFilter}
                            openFilter={colFilters.openFilter}
                            setOpenFilter={colFilters.setOpenFilter}
                            width={colResize.widths[f]}
                            onResize={colResize.setColumnWidth}
                          />
                        ))}
                        {split === "test" &&
                          evalCount > 0 &&
                          outputFields.map((f) => (
                            <ColumnHeader
                              key={`pred-${f}`}
                              label={`pred_${f}`}
                              sortKey={`_pred_${f}`}
                              currentSort={sortKey}
                              sortDir={sortDir}
                              onSort={toggleSort}
                              width={colResize.widths[`_pred_${f}`]}
                              onResize={colResize.setColumnWidth}
                            />
                          ))}
                        {split === "test" &&
                          evalCount > 0 &&
                          loggedMetricNames.map((name) => (
                            <ColumnHeader
                              key={`lm-${name}`}
                              label={name}
                              sortKey={`_lm_${name}`}
                              currentSort={sortKey}
                              sortDir={sortDir}
                              onSort={toggleSort}
                              width={colResize.widths[`_lm_${name}`]}
                              onResize={colResize.setColumnWidth}
                            />
                          ))}
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {filtered.slice(0, 200).map((row, i) => {
                        const ev = currentResults[row.index];
                        return (
                          <TableRow
                            key={row.index}
                            className="cursor-pointer transition-colors duration-150 hover:bg-muted/50"
                            onDoubleClick={() => openReader(i)}
                            onClick={(e) => {
                              if (window.matchMedia("(any-pointer: coarse)").matches) {
                                openReader(i);
                                return;
                              }
                              if (e.detail !== 1) return;
                              const td = (e.target as HTMLElement).closest("td");
                              if (!td || td === td.parentElement?.lastElementChild) return;
                              const text = td.textContent?.trim();
                              if (!text) return;
                              cancelPendingCopy();
                              pendingCopy.current = window.setTimeout(() => {
                                pendingCopy.current = null;
                                navigator.clipboard
                                  .writeText(text)
                                  .then(notifyCopied)
                                  .catch(() => toast.error(msg("clipboard.copy_failed")));
                              }, 250);
                            }}
                          >
                            {split === "test" && evalCount > 0 && (
                              <TableCell
                                className="!p-0 !px-1.5 !py-1"
                                style={
                                  colResize.widths["_score"]
                                    ? { width: colResize.widths["_score"] }
                                    : { width: 72 }
                                }
                              >
                                {ev ? (
                                  <div
                                    className="flex flex-col items-center gap-0.5"
                                    title={
                                      ev.error
                                        ? ev.error
                                        : ev.logged_metrics
                                          ? Object.entries(ev.logged_metrics)
                                              .map(([k, v]) => `${k}: ${Number(v.toFixed(3))}`)
                                              .join(" · ")
                                          : undefined
                                    }
                                  >
                                    <span
                                      className="text-[0.625rem] font-mono tabular-nums font-medium"
                                      style={{ color: scoreColor(ev.score) }}
                                    >
                                      {ev.error ? "⚠ " : ""}
                                      {ev.score.toFixed(2)}
                                    </span>
                                    <ProgressBar
                                      value={ev.score}
                                      max={1}
                                      color={scoreColor(ev.score)}
                                    />
                                  </div>
                                ) : (
                                  <span className="text-[0.625rem] text-border-subtle flex justify-center">
                                    —
                                  </span>
                                )}
                              </TableCell>
                            )}
                            {inputFields.map((f) => (
                              <TableCell
                                key={f}
                                className="text-xs font-mono truncate overflow-hidden"
                                style={
                                  colResize.widths[f]
                                    ? { width: colResize.widths[f], maxWidth: colResize.widths[f] }
                                    : undefined
                                }
                                title={clipText(formatCellValue(row.row[f], true))}
                              >
                                {clipText(formatCellValue(row.row[f]))}
                              </TableCell>
                            ))}
                            {outputFields.map((f) => (
                              <TableCell
                                key={f}
                                className="text-xs font-mono truncate overflow-hidden"
                                style={
                                  colResize.widths[f]
                                    ? { width: colResize.widths[f], maxWidth: colResize.widths[f] }
                                    : undefined
                                }
                                title={clipText(formatCellValue(row.row[f], true))}
                              >
                                {clipText(formatCellValue(row.row[f]))}
                              </TableCell>
                            ))}
                            {split === "test" &&
                              evalCount > 0 &&
                              outputFields.map((f) => {
                                const sigField = Object.entries(
                                  dataset.column_mapping.outputs,
                                ).find(([, col]) => col === f)?.[0];
                                const pred = ev?.outputs[sigField ?? ""];
                                const key = `_pred_${f}`;
                                return (
                                  <TableCell
                                    key={key}
                                    className="text-xs font-mono truncate overflow-hidden"
                                    style={{
                                      ...(colResize.widths[key]
                                        ? {
                                            width: colResize.widths[key],
                                            maxWidth: colResize.widths[key],
                                          }
                                        : {}),
                                      color: ev ? scoreColor(ev.score) : undefined,
                                    }}
                                    title={clipText(formatCellValue(pred, true))}
                                  >
                                    {clipText(formatCellValue(pred))}
                                  </TableCell>
                                );
                              })}
                            {split === "test" &&
                              evalCount > 0 &&
                              loggedMetricNames.map((name) => {
                                const key = `_lm_${name}`;
                                const value = ev?.logged_metrics?.[name];
                                return (
                                  <TableCell
                                    key={key}
                                    className="text-xs font-mono tabular-nums truncate overflow-hidden"
                                    style={{
                                      ...(colResize.widths[key]
                                        ? {
                                            width: colResize.widths[key],
                                            maxWidth: colResize.widths[key],
                                          }
                                        : {}),
                                      // Rate-like values (0–1) reuse the score scale;
                                      // anything else keeps neutral ink.
                                      color:
                                        value != null && value >= 0 && value <= 1
                                          ? scoreColor(value)
                                          : undefined,
                                    }}
                                  >
                                    {value != null ? String(Number(value.toFixed(3))) : ""}
                                  </TableCell>
                                );
                              })}
                          </TableRow>
                        );
                      })}
                    </TableBody>
                    {filtered.length > 200 && (
                      <tfoot>
                        <tr>
                          <td
                            colSpan={99}
                            className="text-center py-3 text-[0.625rem] text-muted-foreground"
                          >
                            {msg("auto.features.optimizations.components.datatab.6")}
                            {filtered.length}
                            {msg("auto.features.optimizations.components.datatab.7")}
                          </td>
                        </tr>
                      </tfoot>
                    )}
                  </Table>
                </div>
              )}
            </CardContent>
          </Card>
        )}
      </FadeIn>
    </div>
  );
}
