"use client";

import { StorageUsageBar } from "@/shared/ui/progress-bar";
import { Fragment, type ReactElement, type ReactNode } from "react";
import Link from "next/link";
import { CaretRight, Coins, Database, Tag } from "@/shared/ui/icons";
import { formatCentsUsd, LinkAccountPrompt, useBalance } from "@/features/billing";
import { useSettingsModal } from "@/features/settings";
import { useHostedFeaturesAvailable } from "@/shared/hooks/use-hosted-features";
import { formatBytes } from "@/shared/lib/formatters";
import { formatMsg, msg } from "@/shared/lib/messages";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import { Skeleton } from "@/shared/ui/skeleton";
import { useWorkspaceSummary } from "../hooks/use-workspace-summary";

/** Chrome shared by the three workspace sections: icon tile, title, count, body. */
function WorkspaceSection({
  icon,
  title,
  count,
  href,
  onOpen,
  children,
}: {
  icon: ReactNode;
  title: string;
  count?: number;
  href?: string;
  onOpen?: () => void;
  children: ReactNode;
}) {
  const header = (
    <span className="flex min-w-0 items-center gap-2">
      <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-accent text-foreground/70">
        {icon}
      </span>
      <span className="truncate text-xs font-semibold text-foreground">{title}</span>
      {count !== undefined && (
        <span className="text-xs font-medium text-muted-foreground tabular-nums">{count}</span>
      )}
      <CaretRight
        aria-hidden="true"
        className="ms-auto size-3.5 shrink-0 text-muted-foreground/40 transition-[color,transform] duration-150 group-hover/ws:translate-x-0.5 group-hover/ws:text-primary rtl:rotate-180 rtl:group-hover/ws:-translate-x-0.5"
      />
    </span>
  );
  return (
    <div className="group/ws min-w-0 p-4 sm:p-5 lg:flex-1">
      {href ? (
        <Link
          href={href}
          className="flex min-h-[44px] items-center rounded-md focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 lg:min-h-0"
        >
          {header}
        </Link>
      ) : (
        <button
          type="button"
          onClick={onOpen}
          className="min-h-[44px] w-full cursor-pointer rounded-md text-start focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 lg:min-h-0"
        >
          {header}
        </button>
      )}
      <div className="mt-3 flex flex-col gap-1.5">{children}</div>
    </div>
  );
}

function ItemRow({ href, name, meta }: { href: string; name: string; meta: string }) {
  return (
    <Link
      href={href}
      className="flex min-h-[44px] items-center justify-between gap-3 rounded-md text-xs hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 lg:min-h-0"
    >
      <span
        className="min-w-0 truncate text-foreground/80 hover:underline underline-offset-2"
        dir="auto"
      >
        {name}
      </span>
      <span className="shrink-0 text-muted-foreground tabular-nums" dir="ltr">
        {meta}
      </span>
    </Link>
  );
}

function EmptyHint({ text }: { text: string }) {
  return <p className="text-xs text-muted-foreground">{text}</p>;
}

function SectionDivider() {
  return (
    <div
      aria-hidden="true"
      className="mx-4 h-px shrink-0 bg-border/50 lg:mx-0 lg:my-4 lg:h-auto lg:w-px"
    />
  );
}

// Header and item rows are links/buttons, which take a 44px floor below lg and
// on any coarse pointer (globals.css), so their bones reserve the same height.
const TAP_ROW_CLASS = "min-h-[44px] lg:min-h-0 any-pointer-coarse:min-h-[44px]";

function RowSkeleton({ nameWidth, link = false }: { nameWidth: number; link?: boolean }) {
  return (
    <div className={cn("flex items-center justify-between gap-3 text-xs", link && TAP_ROW_CLASS)}>
      <Skeleton width={nameWidth} height={12} />
      <Skeleton width={40} height={12} />
    </div>
  );
}

/** Section silhouette at the loaded geometry: icon tile, title, count, body. */
function SectionSkeleton({ children }: { children: ReactNode }) {
  return (
    <div aria-hidden="true" className="min-w-0 p-4 sm:p-5 lg:flex-1">
      <div className={cn("flex items-center gap-2 text-xs", TAP_ROW_CLASS)}>
        <Skeleton width={24} height={24} borderRadius={6} />
        <Skeleton width={104} height={12} />
        <Skeleton width={16} height={12} />
      </div>
      <div className="mt-3 flex flex-col gap-1.5">{children}</div>
    </div>
  );
}

function BalanceSectionSkeleton() {
  return (
    <SectionSkeleton>
      <div className="text-xl leading-none">
        <Skeleton width={64} height={20} />
      </div>
      <RowSkeleton nameWidth={148} />
      <RowSkeleton nameWidth={96} />
      <RowSkeleton nameWidth={120} />
    </SectionSkeleton>
  );
}

/** Loading band at the loaded geometry; the dashboard skeleton reuses it so the two never drift. */
export function WorkspaceStripSkeleton() {
  return (
    <div className="flex flex-col border-t border-border/50 first:border-t-0 lg:flex-row lg:items-stretch">
      {/* Most accounts have no labeling sessions, so this reserves the one-line
          EmptyHint; two session rows would leave 80px to collapse on load. */}
      <SectionSkeleton>
        <div className="flex h-4 items-center">
          <Skeleton width={136} height={12} containerClassName="leading-none" />
        </div>
      </SectionSkeleton>
      <SectionDivider />
      <SectionSkeleton>
        <RowSkeleton nameWidth={112} link />
        <RowSkeleton nameWidth={144} link />
        <div className="mt-1 flex items-center gap-2 text-[0.6875rem]">
          <Skeleton height={4} borderRadius={999} containerClassName="flex-1" />
          <Skeleton width={96} height={11} />
        </div>
      </SectionSkeleton>
      <SectionDivider />
      <BalanceSectionSkeleton />
    </div>
  );
}

/**
 * The workspace band of the dashboard's summary card (rendered under the
 * KPI band), surfacing the surfaces the run-centric dashboard predates:
 * labeling sessions, the dataset library (with its storage meter), and the
 * wallet balance, as divider-separated sections. Every section is one cheap,
 * mostly-cached call — the band renders nothing for a section whose fetch
 * failed rather than blocking the page. While a fetch is in flight the band
 * shows section skeletons at the loaded geometry, so values fill in place
 * instead of the strip popping in.
 */
export function WorkspaceStrip() {
  const { tagging, datasets, loading } = useWorkspaceSummary();
  const { wallet, shown } = useBalance();
  const walletLoading = shown.loading;
  const { openTo } = useSettingsModal();
  const hostedAvailable = useHostedFeaturesAvailable();
  const locale = getActiveIntlLocale();

  if (loading) return <WorkspaceStripSkeleton />;
  if (!tagging && !datasets && walletLoading) return null;

  const usagePct = datasets
    ? Math.min(
        100,
        Math.round((datasets.usage.used_bytes / Math.max(1, datasets.usage.quota_bytes)) * 100),
      )
    : 0;

  const sections: ReactElement[] = [];
  if (tagging) {
    sections.push(
      <WorkspaceSection
        key="tagging"
        icon={<Tag className="size-3.5" aria-hidden="true" />}
        title={msg("dashboard.workspace.tagging.title")}
        count={tagging.total}
        href="/tagger"
      >
        {tagging.recent.length === 0 ? (
          <EmptyHint text={msg("dashboard.workspace.tagging.cta")} />
        ) : (
          tagging.recent.map((s) => (
            <ItemRow
              key={s.id}
              href={`/tagger/${s.id}`}
              name={s.name}
              meta={`${s.tagged_count}/${s.row_count}`}
            />
          ))
        )}
      </WorkspaceSection>,
    );
  }
  if (datasets) {
    sections.push(
      <WorkspaceSection
        key="datasets"
        icon={<Database className="size-3.5" aria-hidden="true" />}
        title={msg("dashboard.workspace.datasets.title")}
        count={datasets.total}
        href="/datasets"
      >
        {datasets.recent.length === 0 ? (
          <EmptyHint text={msg("dashboard.workspace.datasets.cta")} />
        ) : (
          datasets.recent.map((d) => (
            <ItemRow
              key={d.id}
              href={`/datasets/${d.id}/edit?name=${encodeURIComponent(d.name)}`}
              name={d.name}
              meta={formatMsg("datasets.count.rows", { count: d.row_count })}
            />
          ))
        )}
        <div className="mt-1 flex items-center gap-2">
          <StorageUsageBar
            value={usagePct}
            over={datasets.usage.used_bytes > datasets.usage.quota_bytes}
            size="sm"
            className="w-auto flex-1"
          />
          <span className="shrink-0 text-[0.6875rem] text-muted-foreground tabular-nums" dir="ltr">
            {formatBytes(datasets.usage.used_bytes)} / {formatBytes(datasets.usage.quota_bytes)}
          </span>
        </div>
      </WorkspaceSection>,
    );
  }
  // The local build runs on the user's CLI subscriptions; there is nothing to fund.
  const showFundHint = !isLocalMode();
  // An unlinked Mac app has no wallet, so the strip leaves the balance tile out.
  if (hostedAvailable && walletLoading) {
    sections.push(<BalanceSectionSkeleton key="balance" />);
  } else if (hostedAvailable) {
    sections.push(
      <WorkspaceSection
        key="balance"
        icon={<Coins className="size-3.5" aria-hidden="true" />}
        title={msg("dashboard.workspace.balance.title")}
        onOpen={() => openTo("billing")}
      >
        {shown.linkNeeded ? (
          <LinkAccountPrompt />
        ) : (
          <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-1">
            <span
              dir="ltr"
              className="text-xl font-bold leading-none tracking-tight text-foreground tabular-nums"
            >
              {shown.hosted && !shown.available ? "—" : formatCentsUsd(shown.totalCents, locale)}
            </span>
          </div>
        )}
        {wallet.usage.length === 0 ? (
          <>{showFundHint && <EmptyHint text={msg("dashboard.workspace.balance.cta")} />}</>
        ) : (
          wallet.usage.slice(0, 3).map((entry) => (
            <div key={entry.id} className="flex items-baseline justify-between gap-3 text-xs">
              <span className="min-w-0 truncate text-foreground/80" dir="auto">
                {entry.label}
              </span>
              <span
                className={cn(
                  "shrink-0 tabular-nums",
                  entry.cents > 0 ? "text-[var(--success)]" : "text-muted-foreground",
                )}
                dir="ltr"
              >
                {entry.cents > 0 ? "+" : ""}
                {formatCentsUsd(entry.cents, locale)}
              </span>
            </div>
          ))
        )}
      </WorkspaceSection>,
    );
  }
  if (sections.length === 0) return null;

  return (
    <div className="flex flex-col border-t border-border/50 first:border-t-0 lg:flex-row lg:items-stretch">
      {sections.map((section, i) => (
        <Fragment key={section.key}>
          {i > 0 && <SectionDivider />}
          {section}
        </Fragment>
      ))}
    </div>
  );
}
