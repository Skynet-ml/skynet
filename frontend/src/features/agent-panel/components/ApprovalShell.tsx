"use client";

import * as React from "react";
import { CaretDown } from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";

import { cn } from "@/shared/lib/utils";

import { EntryRow } from "./EntryRow";
import type { ApprovalSeverity } from "../lib/tool-meta";

export interface ApprovalStyles {
  border: string;
  bg: string;
  headerBorder: string;
  iconBg: string;
  iconText: string;
  titleText: string;
  descText: string;
  footerBg: string;
}

export const SEVERITY_STYLES: Record<ApprovalSeverity, ApprovalStyles> = {
  destructive: {
    border: "border-[#9B2C1F]/30 dark:border-[#ec7764]/30",
    bg: "bg-white dark:bg-card",
    headerBorder: "border-[#9B2C1F]/20 dark:border-[#ec7764]/20",
    iconBg: "bg-[#9B2C1F]/15",
    iconText: "text-[#9B2C1F] dark:text-[#ffa595]",
    titleText: "text-[#7A1E13] dark:text-[#ffb2a0]",
    descText: "text-[#7A1E13]/80 dark:text-[#ffb2a0]/80",
    footerBg: "bg-white dark:bg-card",
  },
  warning: {
    border: "border-[#A85A1A]/25",
    bg: "bg-white dark:bg-card",
    headerBorder: "border-[#A85A1A]/15",
    iconBg: "bg-[#A85A1A]/15",
    iconText: "text-[#A85A1A] dark:text-[#faa871]",
    titleText: "text-[#7A3E12] dark:text-[#fdb68c]",
    descText: "text-[#7A3E12]/80 dark:text-[#fdb68c]/80",
    footerBg: "bg-white dark:bg-card",
  },
  info: {
    border: "border-border/50",
    bg: "bg-white dark:bg-card",
    headerBorder: "border-border/40",
    iconBg: "bg-foreground/10",
    iconText: "text-foreground/70",
    titleText: "text-foreground",
    descText: "text-muted-foreground",
    footerBg: "bg-white dark:bg-card",
  },
};

interface ApprovalShellProps {
  icon: React.ComponentType<{ className?: string; "aria-hidden"?: boolean | "true" | "false" }>;
  title: string;
  /** The tool call's arguments; none means no Details toggle. */
  entries: Array<[string, unknown]>;
  styles: ApprovalStyles;
  className?: string;
  /** The numbered answers. */
  children: React.ReactNode;
}

/**
 * The shared frame of the approval prompts (SKY-799): a title-only header
 * that opens the tool call's arguments, then the numbered answers.
 *
 * Args:
 *   icon: The header icon.
 *   title: The plain-language action.
 *   entries: The tool call's arguments.
 *   styles: The severity colors.
 *   className: Extra classes for the frame.
 *   children: The answers in the footer.
 *
 * Returns:
 *   The card element.
 */
export function ApprovalShell({ icon: Icon, title, entries, styles, className, children }: ApprovalShellProps) {
  const [open, setOpen] = React.useState(false);
  const panelId = React.useId();
  const hasArgs = entries.length > 0;
  const head = (
    <>
      <span
        className={cn(
          "inline-flex size-7 shrink-0 items-center justify-center rounded-full",
          styles.iconBg,
          styles.iconText,
        )}
      >
        <Icon className="size-3.5" aria-hidden="true" />
      </span>
      <span className={cn("min-w-0 flex-1 truncate text-[0.875rem] font-semibold leading-tight", styles.titleText)}>
        {title}
      </span>
    </>
  );
  const headClass = cn("flex w-full items-center gap-2.5 px-4 py-2.5 border-b", styles.headerBorder);

  return (
    <div className={cn("rounded-md border overflow-hidden", styles.border, styles.bg, className)}>
      {hasArgs ? (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls={panelId}
          className={cn(headClass, "cursor-pointer text-start transition-colors hover:bg-accent/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40")}
        >
          {head}
          <span className={cn("inline-flex shrink-0 items-center gap-1 text-[0.6875rem]", styles.descText)}>
            {msg("agent.approval.details")}
            <CaretDown
              className={cn("size-3 transition-transform motion-reduce:transition-none", open && "rotate-180")}
              aria-hidden="true"
            />
          </span>
        </button>
      ) : (
        <div className={headClass}>{head}</div>
      )}

      {hasArgs && (
        <div
          id={panelId}
          inert={!open}
          className={cn(
            "grid transition-[grid-template-rows] duration-200 ease-out motion-reduce:transition-none",
            open ? "grid-rows-[1fr]" : "grid-rows-[0fr]",
          )}
        >
          <div className="overflow-hidden">
            {/* Every EntryRow root becomes a two-column row, so the labels line up. */}
            <dl
              className={cn(
                "space-y-2 border-b bg-muted/40 px-4 py-3 text-[0.75rem]",
                "[&>div]:grid! [&>div]:grid-cols-[minmax(0,7rem)_minmax(0,1fr)] [&>div]:gap-x-4",
                styles.headerBorder,
              )}
            >
              {entries.map(([key, value]) => (
                <EntryRow key={key} argKey={key} value={value} labelClassName={styles.descText} />
              ))}
            </dl>
          </div>
        </div>
      )}

      <div className={cn("px-2 py-2", styles.footerBg)}>{children}</div>
    </div>
  );
}
