import { STATUS_DOT_COLOR } from "@/shared/constants/job-status";

export const FETCH_PAGE_SIZE = 50;

export const STATUS_COLORS = STATUS_DOT_COLOR;

export type StatAccent = "default" | "success" | "warning" | "danger";

export const ACCENT_DOT: Record<StatAccent, string> = {
  default: "bg-foreground/25",
  success: "bg-[var(--status-success)]",
  warning: "bg-[var(--status-running)]",
  danger: "bg-[var(--status-failed)]",
};

export const ACCENT_TEXT: Record<StatAccent, string> = {
  default: "text-foreground",
  success: "text-[var(--status-success)]",
  warning: "text-[var(--status-running)]",
  danger: "text-[var(--status-failed)]",
};
