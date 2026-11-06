"use client";

import * as React from "react";
import {
  BookOpen,
  CaretRight,
  CheckCircle,
  MagicWand,
  MagnifyingGlass,
  PencilSimple,
  Terminal,
  Warning,
  Wrench,
  type Icon,
} from "@/shared/ui/icons";

import { cn } from "@/shared/lib/utils";
import { formatMsg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import {
  activityGroupOpen,
  joinSummary,
  summarizeActivity,
  type ActivityKind,
  type ActivityStatus,
} from "@/shared/lib/activity-summary";

import { Reveal } from "./reveal";

/** The icon a kind of step shows, on its summary line and on a row with no icon of its own. */
export const ACTIVITY_ICONS: Record<ActivityKind, Icon> = {
  edit: PencilSimple,
  act: MagicWand,
  check: CheckCircle,
  search: MagnifyingGlass,
  read: BookOpen,
  run: Terminal,
  tool: Wrench,
};

const SUMMARY_KEYS = {
  edit: "shared.agent.activity.edit",
  act: "shared.agent.activity.act",
  check: "shared.agent.activity.check",
  search: "shared.agent.activity.search",
  read: "shared.agent.activity.read",
  run: "shared.agent.activity.run",
  tool: "shared.agent.activity.tool",
} as const;

const ERROR_TONE = "text-[var(--danger)]";

interface StepRowProps {
  icon: Icon;
  /** Verb and target, e.g. "Edited metric.py"; present tense while running. */
  label: React.ReactNode;
  /** Muted text after the label: a target, a count, line stats. */
  detail?: React.ReactNode;
  status: ActivityStatus;
  /** Trailing marks after the detail, e.g. a retry glyph. */
  marks?: React.ReactNode;
  /** Read out in place of the warning icon on a failed step. */
  errorLabel?: string;
  /** What the row opens to; a row without it does not open. */
  children?: React.ReactNode;
  /** Makes a row with nothing to open a button that runs this, e.g. showing its file again. */
  onSelect?: () => void;
}

/**
 * One step of an agent's turn as a single muted line, as in Codex: an icon,
 * the verb and its target, cut with an ellipsis. A failed step swaps its icon
 * for a warning. Clicking opens what the step did, plain and indented.
 */
export function StepRow({
  icon: Icon,
  label,
  detail,
  status,
  marks,
  errorLabel,
  children,
  onSelect,
}: StepRowProps) {
  const [open, setOpen] = React.useState(false);
  const canOpen = children !== undefined && children !== null && children !== false;
  const failed = status === "error";
  const line = (
    <>
      {failed ? (
        <Warning
          className={cn("size-3.5 shrink-0", ERROR_TONE)}
          aria-hidden={!errorLabel}
          aria-label={errorLabel}
        />
      ) : (
        <Icon className="size-3.5 shrink-0" aria-hidden />
      )}
      <span className={cn("shrink-0 truncate", status === "running" && "agent-shimmer-text")}>
        {label}
      </span>
      {detail && <span className="min-w-0 truncate opacity-70">{detail}</span>}
      {marks}
      {canOpen && (
        <CaretRight
          className={cn(
            "size-3 shrink-0 transition-[transform,opacity] duration-200 ease-[cubic-bezier(0.16,1,0.3,1)]",
            open ? "rotate-90" : "rtl:-scale-x-100 opacity-0 group-hover:opacity-100",
          )}
          aria-hidden
        />
      )}
    </>
  );
  const lineClass =
    "group flex w-full min-w-0 items-center gap-2 py-1 text-start text-[0.8125rem] text-muted-foreground";
  if (!canOpen && onSelect) {
    return (
      <button
        type="button"
        onClick={onSelect}
        className={cn(lineClass, "cursor-pointer transition-colors hover:text-foreground")}
      >
        {line}
      </button>
    );
  }
  if (!canOpen) return <div className={lineClass}>{line}</div>;
  return (
    <div className="min-w-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(lineClass, "cursor-pointer transition-colors hover:text-foreground", open && "text-foreground")}
      >
        {line}
      </button>
      <Reveal open={open}>
        <div className="space-y-2 pb-2 pe-1 ps-5.5 pt-0.5 text-[0.75rem] leading-snug text-muted-foreground">
          {children}
        </div>
      </Reveal>
    </div>
  );
}

interface ActivityGroupProps {
  /** The kind and status of each step, in the order they ran. */
  steps: ReadonlyArray<{ kind: ActivityKind; status: ActivityStatus }>;
  /** One row per step. */
  children: React.ReactNode;
  /** The burst is still going, so it shows its rows until the reader closes them. */
  live?: boolean;
  /**
   * The rows stay shown once the burst finished too, as the agent panel's
   * history does; only the reader's click folds them.
   */
  stayOpen?: boolean;
  /**
   * Opens by itself while a step runs. Off, the summary line shimmers
   * instead, as Codex's folded groups do, so a quick tool doesn't flash the
   * rows open and shut.
   */
  autoOpen?: boolean;
}

/**
 * A burst of steps folded under one summary line built from their verbs
 * ("Edited a file, ran a check, read results"), as in Codex. The line opens
 * to the rows; it stays open while the burst is live (or, with ``stayOpen``,
 * after it too) unless the reader closes it. A burst of one step is just its row.
 */
export function ActivityGroup({
  steps,
  children,
  live = false,
  stayOpen = false,
  autoOpen = true,
}: ActivityGroupProps) {
  const [choice, setChoice] = React.useState<boolean | null>(null);
  const summary = summarizeActivity(steps);
  if (steps.length <= 1 || !summary.lead) return <div className="flex flex-col">{children}</div>;
  const open = autoOpen
    ? activityGroupOpen(choice, { live, running: summary.running, stayOpen })
    : (choice ?? false);
  const LeadIcon = ACTIVITY_ICONS[summary.lead];
  const label = joinSummary(
    summary.parts.map(({ kind, count }) => formatMsg(SUMMARY_KEYS[kind], { count })),
    getActiveIntlLocale(),
  );
  return (
    <div className="flex flex-col">
      <button
        type="button"
        onClick={() => setChoice(!open)}
        aria-expanded={open}
        className="flex w-full min-w-0 cursor-pointer items-center gap-2 py-1 text-start text-[0.8125rem] text-muted-foreground transition-colors hover:text-foreground"
      >
        {summary.failed ? (
          <Warning className={cn("size-3.5 shrink-0", ERROR_TONE)} aria-hidden />
        ) : (
          <LeadIcon className="size-3.5 shrink-0" aria-hidden />
        )}
        <span className={cn("truncate", summary.running && "agent-shimmer-text")}>{label}</span>
        <CaretRight
          className={cn(
            "size-3 shrink-0 transition-transform duration-200 ease-[cubic-bezier(0.16,1,0.3,1)]",
            open ? "rotate-90" : "rtl:-scale-x-100",
          )}
          aria-hidden
        />
      </button>
      <Reveal open={open}>
        <div className="flex flex-col">{children}</div>
      </Reveal>
    </div>
  );
}
