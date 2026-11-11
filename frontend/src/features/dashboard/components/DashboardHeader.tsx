import { Fragment, useState, type ReactNode } from "react";
import { Users } from "@/shared/ui/icons";
import { AnimatedNumber } from "@/shared/ui/motion";
import type { DashboardStats } from "../lib/get-dashboard-stats";
import { msg } from "@/shared/lib/messages";
import { ACCENT_DOT, ACCENT_TEXT, type StatAccent } from "../constants";

type DashboardHeaderProps = {
  stats: DashboardStats;
};

type StatCellProps = {
  label: string;
  value: number;
  accent?: StatAccent;
  pulse?: boolean;
  icon?: ReactNode;
};

function StatCell({ label, value, accent = "default", pulse = false, icon }: StatCellProps) {
  return (
    <div className="group/stat flex min-w-0 flex-1 flex-col gap-3 px-4 py-4 sm:gap-3.5 sm:px-5 sm:py-5">
      <div className="flex min-w-0 items-center gap-2">
        {icon ?? (
          <span
            className={`size-1.5 shrink-0 rounded-full ${ACCENT_DOT[accent]} ${pulse ? "animate-pulse" : ""}`}
            aria-hidden
          />
        )}
        <p className="truncate text-[0.625rem] font-semibold uppercase tracking-[0.14em] text-muted-foreground/70">
          {label}
        </p>
      </div>
      <p
        className={`text-2xl sm:text-[2rem] font-bold leading-none tracking-tight tabular-nums ${ACCENT_TEXT[accent]}`}
      >
        <AnimatedNumber value={value} />
      </p>
    </div>
  );
}

export function DashboardHeader({ stats: next }: DashboardHeaderProps) {
  // While a tab's stats load, keep the last strip on screen (dimmed) so the
  // page does not collapse and jump back when the numbers land.
  const [shown, setShown] = useState(next);
  if (next && next !== shown) setShown(next);
  const stats = next ?? shown;
  if (!stats) return null;

  const cells: StatCellProps[] = [
    {
      label: msg("auto.features.dashboard.components.dashboardheader.3"),
      value: stats.total,
    },
    {
      label: msg("auto.features.dashboard.components.dashboardheader.4"),
      value: stats.running,
      accent: stats.running > 0 ? "warning" : "default",
      pulse: stats.running > 0,
    },
    {
      label: msg("auto.features.dashboard.components.dashboardheader.6"),
      value: stats.success,
      accent: stats.success > 0 ? "success" : "default",
    },
    {
      label: msg("auto.features.dashboard.components.dashboardheader.7"),
      value: stats.failed,
      accent: stats.failed > 0 ? "danger" : "default",
    },
    {
      label: msg("dashboard.stat.cancelled"),
      value: stats.cancelled,
    },
  ];
  if (stats.stopped > 0) {
    cells.push({
      label: msg("optimization.budget_reached.title"),
      value: stats.stopped,
      accent: "warning",
    });
  }
  if (stats.shared > 0) {
    cells.push({
      label: msg("dashboard.stat.shared"),
      value: stats.shared,
      icon: <Users className="size-3.5 shrink-0 text-muted-foreground/60" aria-hidden />,
    });
  }

  // Sized by the strip's own width, not the viewport: iPad portrait with the
  // sidebar open and Split View leave too little room for five labelled
  // cells in one row, so they wrap to three columns there.
  return (
    <div
      className={`@container transition-opacity duration-200 ${next ? "" : "opacity-50"}`}
      aria-busy={!next}
    >
      <div className="grid grid-cols-2 items-stretch @min-[26rem]:grid-cols-3 @min-[40rem]:flex">
        {cells.map((cell, i) => (
          <Fragment key={cell.label}>
            {i > 0 && (
              <div
                aria-hidden
                className="my-4 hidden w-px shrink-0 bg-border/50 @min-[40rem]:block"
              />
            )}
            <StatCell {...cell} />
          </Fragment>
        ))}
      </div>
    </div>
  );
}
