"use client";

import { clipText } from "@/shared/lib/clip-text";
import { notifyCopied } from "@/shared/lib/notify";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { useTableSort } from "@/shared/hooks/use-table-sort";
import { ArrowDown, CaretRight, Gauge, Scroll } from "@/shared/ui/icons";
import { Card, CardContent } from "@/shared/ui/primitives/card";
import { EmptyState } from "@/shared/ui/empty-state";
import { Badge } from "@/shared/ui/primitives/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableInline,
  TableRow,
} from "@/shared/ui/primitives/table";
import {
  ColumnHeader,
  useColumnFilters,
  useColumnResize,
  ResetColumnsButton,
} from "@/shared/ui/excel-filter";
import { ExportTableMenu } from "@/shared/ui/export-table-menu";
import { FadeIn } from "@/shared/ui/motion";
import { Segmented } from "@/shared/ui/segmented";
import { formatMsg, msg } from "@/shared/lib/messages";
import type { OptimizationLogEntry } from "@/shared/types/api";
import type { RunLogStreamStatus } from "../hooks/use-run-log-stream";
import { formatLogTimestamp, logTimeBucket } from "@/shared/lib";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { defaultOpen, useExperienceLevel } from "@/features/experience";

type Verbosity = "quiet" | "normal" | "verbose";

const SOURCE_LABEL_KEYS: Record<string, string> = {
  host: "optimizations.logs.source.host",
  engine: "optimizations.logs.source.engine",
  proposer: "optimizations.logs.source.proposer",
  scorer: "optimizations.logs.source.scorer",
  sandbox: "optimizations.logs.source.sandbox",
};

function sourceLabel(source: string): string {
  const key = SOURCE_LABEL_KEYS[source];
  return key ? msg(key as Parameters<typeof msg>[0]) : source;
}

/** Rows newer than this many pixels from the newest edge count as "scrolled away". */
const FOLLOW_SLACK_PX = 24;

export function LiveMarker({ status }: { status: RunLogStreamStatus }) {
  if (status !== "live" && status !== "reconnecting") return null;
  const live = status === "live";
  return (
    <span
      role="status"
      className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground"
    >
      <span
        aria-hidden="true"
        className={
          live ? "size-1.5 rounded-full bg-[var(--success)]" : "size-1.5 rounded-full bg-[var(--warning)]"
        }
      />
      {live ? msg("optimizations.logs.live") : msg("optimizations.logs.reconnecting")}
    </span>
  );
}

function levelBadgeVariant(level: string): "destructive" | "outline" | "secondary" {
  return level === "ERROR" ? "destructive" : level === "WARNING" ? "outline" : "secondary";
}

// "verbose" carries no preset — it clears the level filter so every captured
// level (incl. DEBUG) shows. Quiet/Normal map to explicit level sets that drive
// the shared `level` column filter, so the segment and that filter remain one
// source of truth.
const VERBOSITY_LEVELS: Record<Exclude<Verbosity, "verbose">, readonly string[]> = {
  quiet: ["WARNING", "ERROR", "CRITICAL"],
  normal: ["INFO", "WARNING", "ERROR", "CRITICAL"],
};

const VERBOSITY_OPTIONS: ReadonlyArray<{ value: Verbosity; label: () => string }> = [
  { value: "quiet", label: () => msg("optimizations.logs.verbosity.quiet") },
  { value: "normal", label: () => msg("optimizations.logs.verbosity.normal") },
  { value: "verbose", label: () => msg("optimizations.logs.verbosity.verbose") },
];

/** Map the live `level` column-filter set back to the verbosity it represents. */
function verbosityFromLevelFilter(levelSet: Set<string> | undefined): Verbosity | null {
  if (!levelSet || levelSet.size === 0) return "verbose";
  const matches = (levels: readonly string[]) =>
    levels.length === levelSet.size && levels.every((l) => levelSet.has(l));
  if (matches(VERBOSITY_LEVELS.quiet)) return "quiet";
  if (matches(VERBOSITY_LEVELS.normal)) return "normal";
  return null;
}

function VerbosityControl({
  active,
  onChange,
}: {
  active: Verbosity | null;
  onChange: (verbosity: Verbosity) => void;
}) {
  return (
    <div className="inline-flex items-center gap-1.5">
      <Gauge className="size-3 text-foreground/35" aria-hidden="true" />
      <Segmented
        size="sm"
        label={msg("optimizations.logs.verbosity.aria")}
        value={active}
        onChange={(verbosity) => {
          if (verbosity !== active) onChange(verbosity);
        }}
        options={VERBOSITY_OPTIONS.map((o) => ({ value: o.value, label: o.label() }))}
      />
    </div>
  );
}

export function LogsTab({
  logs,
  pairNames,
  liveStatus = "idle",
}: {
  logs: OptimizationLogEntry[];
  pairNames?: Record<number, string>;
  liveStatus?: RunLogStreamStatus;
}) {
  const showPairCol = !!pairNames && Object.keys(pairNames).length > 0;
  const showSourceCol = useMemo(() => logs.some((l) => !!l.source), [logs]);
  const showCandidateCol = useMemo(() => logs.some((l) => !!l.candidate), [logs]);
  const showCaseCol = useMemo(() => logs.some((l) => !!l.case), [logs]);
  const columnCount =
    4 +
    Number(showPairCol) +
    Number(showSourceCol) +
    Number(showCandidateCol) +
    Number(showCaseCol);
  const [openFields, setOpenFields] = useState<ReadonlySet<string>>(() => new Set());
  const toggleFields = (key: string) =>
    setOpenFields((cur) => {
      const next = new Set(cur);
      if (!next.delete(key)) next.add(key);
      return next;
    });
  // Open at the Normal verbosity (INFO+) — DEBUG is captured but hidden until
  // the operator opts into "verbose". Seeding here (vs. an effect) keeps the
  // first paint already filtered, and resets to Normal on every mount.
  // Expert opens at Verbose (no level filter) instead.
  const verboseFirst = defaultOpen("logs.verbose", useExperienceLevel());
  const logFilters = useColumnFilters({
    level: verboseFirst ? new Set<string>() : new Set(VERBOSITY_LEVELS.normal),
  });
  const logResize = useColumnResize();
  const activeVerbosity = useMemo(
    () => verbosityFromLevelFilter(logFilters.filters.level),
    [logFilters.filters.level],
  );
  const setVerbosity = (verbosity: Verbosity) => {
    logFilters.setColumnFilter(
      "level",
      verbosity === "verbose" ? new Set() : new Set(VERBOSITY_LEVELS[verbosity]),
    );
  };
  const { sortKey, sortDir, toggleSort } = useTableSort<string>("timestamp", "desc");

  const filtered = useMemo(() => {
    let result = logs.filter((l) => {
      for (const [col, allowed] of Object.entries(logFilters.filters)) {
        if (allowed.size === 0) continue;
        const val =
          col === "timestamp"
            ? logTimeBucket(l.timestamp)
            : col === "pair_index"
              ? l.pair_index != null
                ? String(l.pair_index)
                : "—"
              : String((l as unknown as Record<string, unknown>)[col] ?? "");
        if (!allowed.has(val)) return false;
      }
      return true;
    });
    const collLocale = getActiveIntlLocale();
    result = [...result].sort((a, b) => {
      let cmp: number;
      if (sortKey === "pair_index") {
        // Numeric column — a direct subtraction beats spinning up the Intl
        // collator on every comparison across a long log table.
        cmp = (a.pair_index ?? -Infinity) - (b.pair_index ?? -Infinity);
      } else if (sortKey === "timestamp") {
        // ISO-8601 timestamps order correctly under plain string comparison,
        // so skip the collator on this hot path too.
        const av = String(a.timestamp ?? "");
        const bv = String(b.timestamp ?? "");
        cmp = av < bv ? -1 : av > bv ? 1 : 0;
      } else {
        // Textual columns (level/logger/message) may hold non-Latin text —
        // locale-aware collation is reserved for these.
        const av = String((a as unknown as Record<string, unknown>)[sortKey] ?? "");
        const bv = String((b as unknown as Record<string, unknown>)[sortKey] ?? "");
        cmp = av.localeCompare(bv, collLocale, { numeric: true });
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return result;
  }, [logs, logFilters.filters, sortKey, sortDir]);

  const filterOptions = useMemo(() => {
    const unique = (key: string) => {
      const vals = [
        ...new Set(logs.map((l) => String((l as unknown as Record<string, unknown>)[key] ?? ""))),
      ]
        .filter(Boolean)
        .sort();
      return vals.map((v) => ({ value: v, label: v }));
    };
    const timestampBuckets = [...new Set(logs.map((l) => logTimeBucket(l.timestamp)))]
      .filter(Boolean)
      .sort()
      .map((v) => ({ value: v, label: v }));
    const pairOpts = showPairCol
      ? [...new Set(logs.map((l) => (l.pair_index != null ? String(l.pair_index) : "—")))]
          .sort()
          .map((v) => ({
            value: v,
            label:
              v === "—"
                ? msg("auto.features.optimizations.components.logstab.literal.1")
                : (pairNames?.[parseInt(v)] ??
                  formatMsg("auto.features.optimizations.components.logstab.template.1", {
                    p1: parseInt(v) + 1,
                  })),
          }))
      : [];
    return {
      level: unique("level"),
      logger: unique("logger"),
      source: unique("source").map((o) => ({ value: o.value, label: sourceLabel(o.value) })),
      candidate: unique("candidate"),
      case: unique("case"),
      timestamp: timestampBuckets,
      pair_index: pairOpts,
    };
  }, [logs, showPairCol, pairNames]);

  // Follow the newest edge while the run streams: the top when newest-first,
  // the bottom when oldest-first. Scrolled away, new rows only bump a counter.
  const scrollRef = useRef<HTMLDivElement>(null);
  const following = liveStatus === "live" || liveStatus === "reconnecting";
  const newestAtTop = sortKey === "timestamp" && sortDir === "desc";
  const newestAtBottom = sortKey === "timestamp" && sortDir === "asc";
  const [awayFromNewest, setAwayFromNewest] = useState(false);
  const [unseen, setUnseen] = useState(0);
  const seenCountRef = useRef(filtered.length);
  const isAwayFromNewest = () => {
    const el = scrollRef.current;
    if (!el) return false;
    if (newestAtTop) return el.scrollTop > FOLLOW_SLACK_PX;
    if (newestAtBottom) return el.scrollHeight - el.clientHeight - el.scrollTop > FOLLOW_SLACK_PX;
    return false;
  };
  const jumpToNewest = () => {
    const el = scrollRef.current;
    if (el) el.scrollTo({ top: newestAtTop ? 0 : el.scrollHeight });
    setUnseen(0);
    setAwayFromNewest(false);
  };
  useEffect(() => {
    const added = filtered.length - seenCountRef.current;
    seenCountRef.current = filtered.length;
    if (!following || added <= 0) return;
    if (awayFromNewest) {
      setUnseen((n) => n + added);
    } else if (newestAtBottom) {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }
  }, [filtered.length]);

  return (
    <div className="mt-4">
      <FadeIn>
        <div className="mb-4 flex flex-col items-stretch gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-3 overflow-x-auto pb-0.5 no-scrollbar sm:overflow-visible sm:pb-0">
            <div className="shrink-0">
              <VerbosityControl active={activeVerbosity} onChange={setVerbosity} />
            </div>
            <ResetColumnsButton resize={logResize} />
            <LiveMarker status={liveStatus} />
          </div>
          <div className="flex shrink-0 items-center justify-between gap-2 sm:justify-end">
            <span className="text-xs text-muted-foreground">
              {filtered.length}
              {msg("auto.features.optimizations.components.logstab.1")}
            </span>
            <ExportTableMenu
              iconOnly
              disabled={filtered.length === 0}
              getData={() => {
                const columns = [
                  ...(showPairCol ? ["pair_index"] : []),
                  "timestamp",
                  "level",
                  ...(showSourceCol ? ["source"] : []),
                  "logger",
                  ...(showCandidateCol ? ["candidate"] : []),
                  ...(showCaseCol ? ["case"] : []),
                  "event",
                  "message",
                  "fields",
                ];
                const rows = filtered.map((log) => {
                  const rec: Record<string, unknown> = {};
                  if (showPairCol) {
                    rec.pair_index =
                      log.pair_index != null
                        ? (pairNames?.[log.pair_index] ??
                          formatMsg("auto.features.optimizations.components.logstab.template.3", {
                            p1: log.pair_index + 1,
                          }))
                        : "—";
                  }
                  rec.timestamp = formatLogTimestamp(log.timestamp);
                  rec.level = log.level;
                  if (showSourceCol) rec.source = log.source ?? "";
                  rec.logger = log.logger;
                  if (showCandidateCol) rec.candidate = log.candidate ?? "";
                  if (showCaseCol) rec.case = log.case ?? "";
                  rec.event = log.event ?? "";
                  rec.message = log.message;
                  rec.fields = log.fields ? JSON.stringify(log.fields) : "";
                  return rec;
                });
                return { columns, rows, filename: "logs" };
              }}
            />
          </div>
        </div>
      </FadeIn>
      {filtered.length === 0 ? (
        <EmptyState
          variant="list"
          icon={Scroll}
          title={
            logs.length === 0
              ? msg("auto.features.optimizations.components.logstab.2")
              : activeVerbosity === "quiet" &&
                  Object.keys(logFilters.filters).length === 1 &&
                  !!logFilters.filters.level
                ? msg("optimizations.logs.verbosity.empty_quiet")
                : msg("optimizations.logs.verbosity.empty_filtered")
          }
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="relative">
              {unseen > 0 && (
                <button
                  type="button"
                  onClick={jumpToNewest}
                  className={`absolute start-1/2 z-10 inline-flex -translate-x-1/2 rtl:translate-x-1/2 items-center gap-1.5 rounded-full border border-border bg-background px-3 py-1 text-xs text-foreground transition-colors duration-150 hover:bg-muted ${newestAtTop ? "top-10" : "bottom-3"}`}
                >
                  <ArrowDown
                    className={newestAtTop ? "size-3 rotate-180" : "size-3"}
                    aria-hidden="true"
                  />
                  {formatMsg("optimizations.logs.new_lines", { count: unseen })}
                  <span className="text-muted-foreground">·</span>
                  {msg("optimizations.logs.jump_latest")}
                </button>
              )}
              <div
                ref={scrollRef}
                className="max-h-[600px] overflow-auto"
                onScroll={() => {
                  const away = isAwayFromNewest();
                  if (away !== awayFromNewest) setAwayFromNewest(away);
                  if (!away && unseen > 0) setUnseen(0);
                }}
              >
                <Table className="w-full table-fixed">
                  <colgroup>
                    {showPairCol && (
                      <col
                        data-collapse="lg"
                        style={{ width: logResize.widths["pair_index"] ?? "12%" }}
                      />
                    )}
                    <col
                      data-collapse="md"
                      style={{
                        width: logResize.widths["timestamp"] ?? (showPairCol ? "13%" : "15%"),
                      }}
                    />
                    <col
                      data-collapse="sm"
                      style={{ width: logResize.widths["level"] ?? (showPairCol ? "10%" : "12%") }}
                    />
                    {showSourceCol && (
                      <col
                        data-collapse="md"
                        style={{ width: logResize.widths["source"] ?? "9%" }}
                      />
                    )}
                    <col
                      data-collapse="lg"
                      style={{ width: logResize.widths["logger"] ?? (showPairCol ? "14%" : "17%") }}
                    />
                    {showCandidateCol && (
                      <col
                        data-collapse="lg"
                        style={{ width: logResize.widths["candidate"] ?? "9%" }}
                      />
                    )}
                    {showCaseCol && (
                      <col data-collapse="lg" style={{ width: logResize.widths["case"] ?? "7%" }} />
                    )}
                    <col />
                  </colgroup>
                  <TableHeader>
                    <TableRow>
                      {showPairCol && (
                        <ColumnHeader
                          label={msg("auto.features.optimizations.components.logstab.literal.2")}
                          sortKey="pair_index"
                          currentSort={sortKey}
                          sortDir={sortDir}
                          onSort={toggleSort}
                          filterCol="pair_index"
                          filterOptions={filterOptions.pair_index}
                          filters={logFilters.filters}
                          onFilter={logFilters.setColumnFilter}
                          openFilter={logFilters.openFilter}
                          setOpenFilter={logFilters.setOpenFilter}
                          width={logResize.widths["pair_index"]}
                          onResize={logResize.setColumnWidth}
                          collapse="lg"
                        />
                      )}
                      <ColumnHeader
                        label={msg("auto.features.optimizations.components.logstab.literal.3")}
                        sortKey="timestamp"
                        currentSort={sortKey}
                        sortDir={sortDir}
                        onSort={toggleSort}
                        filterCol="timestamp"
                        filterOptions={filterOptions.timestamp}
                        filters={logFilters.filters}
                        onFilter={logFilters.setColumnFilter}
                        openFilter={logFilters.openFilter}
                        setOpenFilter={logFilters.setOpenFilter}
                        width={logResize.widths["timestamp"]}
                        onResize={logResize.setColumnWidth}
                        collapse="md"
                      />
                      <ColumnHeader
                        label={msg("auto.features.optimizations.components.logstab.literal.4")}
                        sortKey="level"
                        currentSort={sortKey}
                        sortDir={sortDir}
                        onSort={toggleSort}
                        filterCol="level"
                        filterOptions={filterOptions.level}
                        filters={logFilters.filters}
                        onFilter={logFilters.setColumnFilter}
                        openFilter={logFilters.openFilter}
                        setOpenFilter={logFilters.setOpenFilter}
                        width={logResize.widths["level"]}
                        onResize={logResize.setColumnWidth}
                        collapse="sm"
                      />
                      {showSourceCol && (
                        <ColumnHeader
                          label={msg("optimizations.logs.col.source")}
                          sortKey="source"
                          currentSort={sortKey}
                          sortDir={sortDir}
                          onSort={toggleSort}
                          filterCol="source"
                          filterOptions={filterOptions.source}
                          filters={logFilters.filters}
                          onFilter={logFilters.setColumnFilter}
                          openFilter={logFilters.openFilter}
                          setOpenFilter={logFilters.setOpenFilter}
                          width={logResize.widths["source"]}
                          onResize={logResize.setColumnWidth}
                          collapse="md"
                        />
                      )}
                      <ColumnHeader
                        label={msg("auto.features.optimizations.components.logstab.literal.5")}
                        sortKey="logger"
                        currentSort={sortKey}
                        sortDir={sortDir}
                        onSort={toggleSort}
                        filterCol="logger"
                        filterOptions={filterOptions.logger}
                        filters={logFilters.filters}
                        onFilter={logFilters.setColumnFilter}
                        openFilter={logFilters.openFilter}
                        setOpenFilter={logFilters.setOpenFilter}
                        width={logResize.widths["logger"]}
                        onResize={logResize.setColumnWidth}
                        collapse="lg"
                      />
                      {showCandidateCol && (
                        <ColumnHeader
                          label={msg("optimizations.logs.col.candidate")}
                          sortKey="candidate"
                          currentSort={sortKey}
                          sortDir={sortDir}
                          onSort={toggleSort}
                          filterCol="candidate"
                          filterOptions={filterOptions.candidate}
                          filters={logFilters.filters}
                          onFilter={logFilters.setColumnFilter}
                          openFilter={logFilters.openFilter}
                          setOpenFilter={logFilters.setOpenFilter}
                          width={logResize.widths["candidate"]}
                          onResize={logResize.setColumnWidth}
                          collapse="lg"
                        />
                      )}
                      {showCaseCol && (
                        <ColumnHeader
                          label={msg("optimizations.logs.col.case")}
                          sortKey="case"
                          currentSort={sortKey}
                          sortDir={sortDir}
                          onSort={toggleSort}
                          filterCol="case"
                          filterOptions={filterOptions.case}
                          filters={logFilters.filters}
                          onFilter={logFilters.setColumnFilter}
                          openFilter={logFilters.openFilter}
                          setOpenFilter={logFilters.setOpenFilter}
                          width={logResize.widths["case"]}
                          onResize={logResize.setColumnWidth}
                          collapse="lg"
                        />
                      )}
                      <ColumnHeader
                        label={msg("auto.features.optimizations.components.logstab.literal.6")}
                        sortKey="message"
                        currentSort={sortKey}
                        sortDir={sortDir}
                        onSort={toggleSort}
                        width={logResize.widths["message"]}
                        onResize={logResize.setColumnWidth}
                      />
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {filtered.slice(0, 300).map((log, i) => {
                      const rowKey = log.id != null ? String(log.id) : `i${i}`;
                      const hasFields = !!log.fields && Object.keys(log.fields).length > 0;
                      const fieldsOpen = hasFields && openFields.has(rowKey);
                      return (
                        <Fragment key={rowKey}>
                          <TableRow
                            className="cursor-pointer transition-colors duration-150 hover:bg-muted/50"
                            onClick={(e) => {
                              const td = (e.target as HTMLElement).closest("td");
                              if (!td) return;
                              // The message cell also holds folded copies of the
                              // collapsible columns (present in textContent even
                              // while hidden), so copy just the message there.
                              const text = (
                                td.querySelector("[data-log-message]") ?? td
                              ).textContent?.trim();
                              if (text) {
                                void navigator.clipboard.writeText(text);
                                notifyCopied();
                              }
                            }}
                          >
                            {showPairCol && (
                              <TableCell
                                collapse="lg"
                                className="text-xs font-mono truncate overflow-hidden"
                                style={
                                  logResize.widths["pair_index"]
                                    ? {
                                        width: logResize.widths["pair_index"],
                                        maxWidth: logResize.widths["pair_index"],
                                      }
                                    : undefined
                                }
                              >
                                {log.pair_index != null ? (
                                  <Badge variant="secondary" size="sm" className="font-mono">
                                    {pairNames?.[log.pair_index] ??
                                      formatMsg(
                                        "auto.features.optimizations.components.logstab.template.3",
                                        { p1: log.pair_index + 1 },
                                      )}
                                  </Badge>
                                ) : (
                                  <span className="text-muted-foreground/40">—</span>
                                )}
                              </TableCell>
                            )}
                            <TableCell
                              collapse="md"
                              className="text-xs font-mono text-muted-foreground truncate overflow-hidden"
                              style={
                                logResize.widths["timestamp"]
                                  ? {
                                      width: logResize.widths["timestamp"],
                                      maxWidth: logResize.widths["timestamp"],
                                    }
                                  : undefined
                              }
                              dir="ltr"
                            >
                              {formatLogTimestamp(log.timestamp)}
                            </TableCell>
                            <TableCell
                              collapse="sm"
                              className="truncate overflow-hidden"
                              style={
                                logResize.widths["level"]
                                  ? {
                                      width: logResize.widths["level"],
                                      maxWidth: logResize.widths["level"],
                                    }
                                  : undefined
                              }
                            >
                              <Badge
                                variant={levelBadgeVariant(log.level)}
                                className="text-[0.625rem] font-mono"
                              >
                                {log.level}
                              </Badge>
                            </TableCell>
                            {showSourceCol && (
                              <TableCell
                                collapse="md"
                                className="text-xs font-mono text-muted-foreground truncate overflow-hidden"
                                style={
                                  logResize.widths["source"]
                                    ? {
                                        width: logResize.widths["source"],
                                        maxWidth: logResize.widths["source"],
                                      }
                                    : undefined
                                }
                                title={log.source ?? undefined}
                              >
                                {log.source ? sourceLabel(log.source) : ""}
                              </TableCell>
                            )}
                            <TableCell
                              collapse="lg"
                              className="text-xs font-mono text-muted-foreground truncate overflow-hidden"
                              style={
                                logResize.widths["logger"]
                                  ? {
                                      width: logResize.widths["logger"],
                                      maxWidth: logResize.widths["logger"],
                                    }
                                  : undefined
                              }
                              title={log.logger}
                            >
                              {log.logger}
                            </TableCell>
                            {showCandidateCol && (
                              <TableCell
                                collapse="lg"
                                className="text-xs font-mono text-muted-foreground truncate overflow-hidden"
                                style={
                                  logResize.widths["candidate"]
                                    ? {
                                        width: logResize.widths["candidate"],
                                        maxWidth: logResize.widths["candidate"],
                                      }
                                    : undefined
                                }
                                title={log.candidate ?? undefined}
                              >
                                {log.candidate}
                              </TableCell>
                            )}
                            {showCaseCol && (
                              <TableCell
                                collapse="lg"
                                className="text-xs font-mono text-muted-foreground truncate overflow-hidden"
                                style={
                                  logResize.widths["case"]
                                    ? {
                                        width: logResize.widths["case"],
                                        maxWidth: logResize.widths["case"],
                                      }
                                    : undefined
                                }
                                title={log.case ?? undefined}
                              >
                                {log.case}
                              </TableCell>
                            )}
                            <TableCell
                              className="text-xs font-mono whitespace-pre-wrap break-all overflow-hidden hover:underline"
                              style={
                                logResize.widths["message"]
                                  ? {
                                      width: logResize.widths["message"],
                                      maxWidth: logResize.widths["message"],
                                    }
                                  : undefined
                              }
                              title={clipText(log.message)}
                            >
                              <div className="flex flex-wrap items-center gap-x-2">
                                {showPairCol && log.pair_index != null && (
                                  <TableInline at="lg" className="mb-1">
                                    <Badge variant="secondary" size="sm" className="font-mono">
                                      {pairNames?.[log.pair_index] ??
                                        formatMsg(
                                          "auto.features.optimizations.components.logstab.template.3",
                                          { p1: log.pair_index + 1 },
                                        )}
                                    </Badge>
                                  </TableInline>
                                )}
                                <TableInline at="md" className="mb-1" dir="ltr">
                                  {formatLogTimestamp(log.timestamp)}
                                </TableInline>
                                <TableInline at="sm" className="mb-1">
                                  <Badge
                                    variant={levelBadgeVariant(log.level)}
                                    className="text-[0.625rem] font-mono"
                                  >
                                    {log.level}
                                  </Badge>
                                </TableInline>
                                {showSourceCol && log.source && (
                                  <TableInline at="md" className="mb-1 text-muted-foreground">
                                    {sourceLabel(log.source)}
                                  </TableInline>
                                )}
                                <TableInline
                                  at="lg"
                                  className="mb-1 min-w-0 shrink truncate"
                                  title={log.logger}
                                >
                                  {log.logger}
                                </TableInline>
                                {showCandidateCol && log.candidate && (
                                  <TableInline at="lg" className="mb-1 text-muted-foreground">
                                    {log.candidate}
                                  </TableInline>
                                )}
                                {showCaseCol && log.case && (
                                  <TableInline at="lg" className="mb-1 text-muted-foreground">
                                    {log.case}
                                  </TableInline>
                                )}
                              </div>
                              {log.event && (
                                <Badge variant="outline" size="sm" className="me-1.5 font-mono">
                                  {log.event}
                                </Badge>
                              )}
                              <span data-log-message>{log.message}</span>
                              {hasFields && (
                                <button
                                  type="button"
                                  aria-expanded={fieldsOpen}
                                  aria-label={
                                    fieldsOpen
                                      ? msg("optimizations.logs.fields.hide")
                                      : msg("optimizations.logs.fields.show")
                                  }
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    toggleFields(rowKey);
                                  }}
                                  className="ms-1.5 inline-flex size-5 cursor-pointer items-center justify-center rounded align-middle text-muted-foreground transition-colors duration-150 hover:bg-muted hover:text-foreground"
                                >
                                  <CaretRight
                                    className={`size-3 transition-transform duration-150 rtl:-scale-x-100 ${fieldsOpen ? "rotate-90 rtl:-rotate-90" : ""}`}
                                    aria-hidden="true"
                                  />
                                </button>
                              )}
                            </TableCell>
                          </TableRow>
                          {fieldsOpen && (
                            <TableRow className="hover:bg-transparent">
                              <TableCell colSpan={columnCount} className="bg-muted/30 py-2">
                                <pre
                                  dir="ltr"
                                  className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-start text-[0.6875rem] font-mono text-muted-foreground"
                                >
                                  {JSON.stringify(log.fields, null, 2)}
                                </pre>
                              </TableCell>
                            </TableRow>
                          )}
                        </Fragment>
                      );
                    })}
                  </TableBody>
                  {filtered.length > 300 && (
                    <tfoot>
                      <tr>
                        <td
                          colSpan={columnCount}
                          className="text-center py-3 text-[0.625rem] text-muted-foreground"
                        >
                          {msg("auto.features.optimizations.components.logstab.3")}
                          {filtered.length}
                          {msg("auto.features.optimizations.components.logstab.4")}
                        </td>
                      </tr>
                    </tfoot>
                  )}
                </Table>
              </div>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
