import { STATUS_DOT_COLOR } from "@/shared/constants/job-status";

export const FETCH_PAGE_SIZE = 50;

// The empty Optimizations and Usage cards share these, so both cards are one
// size and nothing moves when the user switches tabs. The toolbar row keeps
// its height when a reset button shows, and the empty state has room for a
// two-line description (the Usage one wraps in English).
export const CARD_TOOLBAR_HEIGHT = "min-h-[44px] lg:min-h-8";
export const EMPTY_CARD_STATE = "min-h-[300px] justify-center";

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
