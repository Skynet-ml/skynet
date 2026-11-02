import type { CSSProperties } from "react";
import { cn } from "@/shared/lib/utils";

// The underline row has no padding or gaps, so each trigger takes an equal
// share of the width; the underline mirrors the active one.
export function underlineTabStyle(index: number, count: number): CSSProperties {
  return {
    width: `calc(100% / ${count})`,
    insetInlineStart: `calc(${index} * 100% / ${count})`,
  };
}

export function Stat({
  label,
  value,
  tone,
  dir,
}: {
  label: string;
  value: string;
  tone?: "ok" | "bad";
  dir?: "ltr";
}) {
  return (
    <div className="flex-1 min-w-0 px-2.5 py-1.5 border-s border-border/30 first:border-s-0">
      <div className="text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground truncate">
        {label}
      </div>
      <div
        className={cn(
          "mt-0.5 font-mono text-[13px] font-medium tabular-nums leading-tight truncate",
          tone === "ok" && "text-[#5f6f2f] dark:text-[#b4c785]",
          tone === "bad" && "text-[#a85a3b] dark:text-[#fba584]",
        )}
        title={value}
        dir={dir}
      >
        {value}
      </div>
    </div>
  );
}
