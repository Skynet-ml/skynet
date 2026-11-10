"use client";

import * as React from "react";
import { CaretLeft, CaretRight, Database, HardDrive, WarningCircle } from "@/shared/ui/icons";
import { getStorageUsage, type StorageUsageResponse } from "@/shared/lib/api";
import { EmptyState } from "@/shared/ui/empty-state";
import { formatStorageSize } from "@/shared/lib/formatters";
import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { StorageCategoryDrawer } from "./StorageCategoryDrawer";
import { StackedUsageBar } from "./StackedUsageBar";
import { StorageSkeleton } from "./StorageSkeleton";
import { CATEGORY_META, STORAGE_CATEGORY_ORDER } from "../lib/categories";
import { cn } from "@/shared/lib/utils";
import { useResourceMode } from "@/shared/ui/resource-mode-switch";
import { useSourceSwap } from "@/shared/hooks/use-source-swap";

/**
 * Top-level /storage page: the account-wide cleanup surface. A usage gauge over
 * the caller's budget, then a per-category breakdown where each category opens a
 * drawer listing all of its items for in-place deletion. Byproduct bytes (logs,
 * progress events, embeddings) are folded by the backend into the footprint of
 * the optimization or chat that owns them, so every row here is deletable.
 */
export function StorageView() {
  const [usage, setUsage] = React.useState<StorageUsageResponse | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState(false);
  const [openCategory, setOpenCategory] = React.useState<string | null>(null);

  const OpenIcon = getActiveDir() === "rtl" ? CaretLeft : CaretRight;
  const resources = useResourceMode();
  const source = resources.mode;
  // Deletes from this page act on this Mac's data only, so web rows stay read-only here.
  const remote = resources.switchable && source === "hosted";
  const swapping = useSourceSwap(source);

  // The newest request wins, so a late answer for the other mode never
  // replaces the figures on screen.
  const reqId = React.useRef(0);
  const refreshUsage = React.useCallback(() => {
    const id = ++reqId.current;
    getStorageUsage(source)
      .then((res) => {
        if (id === reqId.current) setUsage(res);
      })
      .catch(() => {
        /* keep the last figure rather than blanking the gauge */
      });
  }, [source]);

  React.useEffect(() => {
    let cancelled = false;
    const id = ++reqId.current;
    setLoading(true);
    setError(false);
    getStorageUsage(source)
      .then((res) => {
        if (!cancelled && id === reqId.current) setUsage(res);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [source]);

  // Every category is listed, so the user sees each kind of data the app keeps
  // even before it holds anything; the biggest come first, empty ones last.
  const rows = React.useMemo(() => {
    if (!usage) return [];
    return STORAGE_CATEGORY_ORDER.map((key) => ({
      key,
      bytes: usage.breakdown[key] ?? 0,
      count: usage.counts?.[key],
    })).sort((a, b) => b.bytes - a.bytes);
  }, [usage]);

  if (loading || swapping) return <StorageSkeleton />;

  if (error || !usage) {
    return (
      <div className="pb-16">
        <div className="mt-8">
          <EmptyState icon={HardDrive} title={msg("storage.page.error")} />
        </div>
      </div>
    );
  }

  const used = usage.used_bytes;
  const quota = usage.quota_bytes;
  const usagePct = quota > 0 ? Math.min(100, (used / quota) * 100) : 0;
  const free = Math.max(0, quota - used);

  return (
    <div className="swap-in pb-16">
      <section className="mt-8">
        <div className="flex items-baseline justify-between gap-3">
          <p className="text-foreground">
            <span className="text-2xl font-semibold tabular-nums">{formatStorageSize(used)}</span>
            <span className="ms-1.5 text-sm text-muted-foreground">
              {formatMsg("storage.page.of_total", { total: formatStorageSize(quota) })}
            </span>
          </p>
          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
            {formatMsg("storage.page.free", { free: formatStorageSize(free) })}
          </span>
        </div>
        <StackedUsageBar rows={rows} quota={quota} className="mt-3" />
        {quota > 0 && used >= quota && (
          <p role="alert" className="mt-3 flex items-start gap-1.5 text-sm text-destructive">
            <WarningCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            {msg("storage.quota.full_warning")}
          </p>
        )}
        <p className="mt-2 text-xs tabular-nums text-muted-foreground">
          {formatMsg("storage.page.percent", {
            percent: used > 0 ? Math.max(1, Math.round(usagePct)) : 0,
          })}
        </p>
      </section>

      <section className="mt-10">
        <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
          <Database className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          {msg("storage.breakdown.title")}
        </h2>
        {remote && (
          <p className="mt-1 text-xs text-muted-foreground">{msg("storage.page.remote_note")}</p>
        )}
        <ul className="mt-3 flex flex-col divide-y divide-border-subtle/60">
          {rows.map(({ key, bytes, count }) => {
            const meta = CATEGORY_META[key];
            const label = msg(meta.label);
            const CategoryIcon = meta.icon;
            const empty = bytes === 0 && !count;
            const share = used > 0 ? Math.round((bytes / used) * 100) : 0;
            const countText =
              count == null
                ? null
                : count === 1
                  ? msg("storage.category.count_one")
                  : formatMsg("storage.category.count", { n: count });

            const body = (
              <>
                <span
                  className={cn(
                    "flex size-9 shrink-0 items-center justify-center rounded-lg",
                    empty ? "bg-muted/50 text-muted-foreground/60" : "bg-muted text-foreground",
                  )}
                >
                  <CategoryIcon className="size-4" aria-hidden="true" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-2 text-sm font-medium text-foreground">
                    <span
                      className={cn(
                        "size-2 shrink-0 rounded-full",
                        empty ? "bg-border" : meta.swatch,
                      )}
                      aria-hidden="true"
                    />
                    <span className="truncate">{label}</span>
                  </span>
                  <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                    {msg(meta.description)}
                  </span>
                </span>
                <span className="shrink-0 text-end">
                  <span
                    className={cn(
                      "block text-sm tabular-nums",
                      empty ? "text-muted-foreground" : "font-medium text-foreground",
                    )}
                  >
                    {empty ? msg("storage.category.empty") : formatStorageSize(bytes)}
                  </span>
                  {!empty && (
                    <span className="mt-0.5 block text-xs tabular-nums text-muted-foreground">
                      {[countText, formatMsg("storage.category.share", { percent: share })]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  )}
                </span>
              </>
            );

            return (
              <li key={key}>
                {empty || remote ? (
                  <div
                    className={cn(
                      "flex min-h-14 items-center gap-3 px-2 py-2.5",
                      empty && "opacity-70",
                    )}
                  >
                    {body}
                  </div>
                ) : (
                  <button
                    type="button"
                    onClick={() => setOpenCategory(key)}
                    aria-label={formatMsg("storage.category.open", { category: label })}
                    className="group flex min-h-14 w-full cursor-pointer items-center gap-3 rounded-lg px-2 py-2.5 text-start transition-colors duration-150 hover:bg-muted/50"
                  >
                    {body}
                    <OpenIcon
                      className="size-4 shrink-0 text-muted-foreground/60 transition-transform duration-150 group-hover:translate-x-0.5 rtl:group-hover:-translate-x-0.5"
                      aria-hidden="true"
                    />
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      <StorageCategoryDrawer
        category={openCategory}
        onClose={() => setOpenCategory(null)}
        onChanged={refreshUsage}
      />
    </div>
  );
}
