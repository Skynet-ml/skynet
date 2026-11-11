"use client";

import { StorageUsageBar } from "@/shared/ui/progress-bar";
import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { HardDrive, WarningCircle } from "@/shared/ui/icons";
import {
  getStorageUsage,
  STORAGE_CHANGED_EVENT,
  type StorageUsageResponse,
} from "@/shared/lib/api";
import { formatStorageSize } from "@/shared/lib/formatters";
import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { cn } from "@/shared/lib/utils";
import { useResourceMode } from "@/shared/ui/resource-mode-switch";
import { useSourceSwap } from "@/shared/hooks/use-source-swap";
import { Skeleton } from "@/shared/ui/skeleton";

/**
 * Compact account-wide storage gauge for the sidebar footer: a thin bar plus an
 * "X of Y used" caption against the caller's storage budget, linking to the
 * /storage cleanup page.
 *
 * Fetches the unified usage figure once on mount, again whenever the tab regains
 * focus, and whenever a {@link STORAGE_CHANGED_EVENT} fires (a delete on the
 * storage page), so the figure stays current without a reload. Renders nothing
 * until the first figure resolves (and on error), so it never flashes an empty
 * bar in the always-visible footer. When ``collapsed`` (the icon rail), it
 * shrinks to a lone drive icon whose tooltip carries the same usage figure.
 */
export function StorageMeter({ collapsed = false }: { collapsed?: boolean }) {
  const [usage, setUsage] = React.useState<StorageUsageResponse | null>(null);
  const active = usePathname().startsWith("/storage");
  const source = useResourceMode().mode;
  const [usageSource, setUsageSource] = React.useState(source);
  const swapping = useSourceSwap(source);

  React.useEffect(() => {
    let cancelled = false;
    // The newest request wins, so a late answer for the other mode never shows.
    let reqId = 0;
    const load = () => {
      const id = ++reqId;
      getStorageUsage(source)
        .then((res) => {
          if (!cancelled && id === reqId) {
            setUsage(res);
            setUsageSource(source);
          }
        })
        .catch(() => {
          /* ambient gauge — stay silent if the figure is briefly unavailable */
        });
    };
    load();
    const onVisible = () => {
      if (document.visibilityState === "visible") load();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener(STORAGE_CHANGED_EVENT, load);
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener(STORAGE_CHANGED_EVENT, load);
    };
  }, [source]);

  if (!usage || usage.quota_bytes <= 0) return null;

  if (!collapsed && (swapping || usageSource !== source)) {
    return (
      <div className="min-h-[44px] px-3 pt-3 pb-1 lg:min-h-0" aria-hidden="true">
        <Skeleton height={6} borderRadius={9999} containerClassName="flex leading-none" />
        <p className="mt-1.5 text-[0.6875rem]">
          <Skeleton width="60%" />
        </p>
      </div>
    );
  }

  const usagePct = Math.min(100, (usage.used_bytes / usage.quota_bytes) * 100);
  // Full, not over: run logs stop saving the moment usage reaches the quota.
  const full = usage.used_bytes >= usage.quota_bytes;
  const usageText = formatMsg("storage.quota.usage", {
    used: formatStorageSize(usage.used_bytes),
    total: formatStorageSize(usage.quota_bytes),
  });
  const usageLabel = full ? `${usageText}. ${msg("storage.quota.full_warning")}` : usageText;

  if (collapsed) {
    const tooltipSide = getActiveDir() === "rtl" ? "left" : "right";
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Link
            href="/storage"
            aria-label={usageLabel}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex min-h-[44px] min-w-[44px] items-center justify-center rounded-lg px-2 py-2.5 transition-colors duration-150 lg:min-h-0 lg:min-w-0",
              active ? "bg-sidebar-accent text-sidebar-foreground" : "hover:bg-sidebar-accent/40",
            )}
          >
            {full ? (
              <WarningCircle className={cn("size-4", "text-destructive")} />
            ) : (
              <HardDrive
                className={cn(
                  "size-4",
                  active ? "text-sidebar-foreground" : "text-muted-foreground",
                )}
              />
            )}
          </Link>
        </TooltipTrigger>
        <TooltipContent side={tooltipSide}>{usageLabel}</TooltipContent>
      </Tooltip>
    );
  }

  return (
    <Link
      href="/storage"
      aria-label={msg("storage.page.title")}
      aria-current={active ? "page" : undefined}
      className={cn(
        "swap-in block min-h-[44px] rounded-lg px-3 pt-3 pb-1 transition-colors duration-150 lg:min-h-0",
        active ? "bg-sidebar-accent" : "hover:bg-sidebar-accent/40",
      )}
    >
      <StorageUsageBar value={usagePct} over={usage.used_bytes > usage.quota_bytes} />
      <p className="mt-1.5 flex items-center gap-1 text-start text-[0.6875rem] text-muted-foreground tabular-nums">
        {full && <WarningCircle className="size-3 shrink-0 text-destructive" aria-hidden="true" />}
        {usageText}
      </p>
      {full && (
        <p role="status" className="mt-0.5 text-start text-[0.6875rem] text-destructive">
          {msg("storage.quota.full_warning")}
        </p>
      )}
    </Link>
  );
}
