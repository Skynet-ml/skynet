import type { ComponentProps } from "react";
import { cn } from "@/shared/lib/utils";

const TONE_CLASS = {
  primary: "bg-primary-fill",
  ai: "",
  success: "bg-[var(--success)]",
  warning: "bg-[var(--warning)]",
  danger: "bg-destructive",
} as const;

interface ProgressBarProps extends Omit<ComponentProps<"div">, "color"> {
  /** Progress on a 0–`max` scale; clamped. */
  value: number;
  max?: number;
  /** `md` = h-1.5 (default), `sm` = h-1. */
  size?: "sm" | "md";
  tone?: keyof typeof TONE_CLASS;
  /** Custom fill color (chart share bars); overrides `tone`. */
  color?: string;
  fillClassName?: string;
}

/** Linear progress bar or share meter in the BoardUI style. Extra children (e.g. a threshold marker) render inside the track. */
export function ProgressBar({
  value,
  max = 100,
  size = "md",
  tone = "primary",
  color,
  className,
  fillClassName,
  children,
  ...props
}: ProgressBarProps) {
  const pct = max > 0 ? Math.max(0, Math.min(1, value / max)) * 100 : 0;
  const background = color ?? (tone === "ai" ? "var(--gradient-progress)" : undefined);
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={Math.round(value)}
      className={cn(
        "relative w-full overflow-hidden rounded-full bg-border",
        size === "sm" ? "h-1" : "h-1.5",
        className,
      )}
      {...props}
    >
      <div
        className={cn(
          "h-full rounded-full transition-[width] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] motion-reduce:transition-none",
          color == null && TONE_CLASS[tone],
          fillClassName,
        )}
        style={{ width: `${pct}%`, ...(background ? { background } : null) }}
      />
      {children}
    </div>
  );
}

interface StorageUsageBarProps extends Omit<ProgressBarProps, "tone" | "color"> {
  /** Usage exceeds the quota/budget — fill turns destructive. */
  over?: boolean;
}

/** Storage/quota usage meter: border-tone rail, primary fill, destructive when over quota. */
export function StorageUsageBar({
  over = false,
  className,
  fillClassName,
  ...props
}: StorageUsageBarProps) {
  return (
    <ProgressBar
      className={className}
      fillClassName={cn(over ? "bg-destructive" : "bg-primary-fill", fillClassName)}
      {...props}
    />
  );
}
