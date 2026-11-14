// The review pane's menu style: dense 28px rows, a switch on setting rows,
// and quiet group labels.

import * as React from "react";

import { cn } from "@/shared/lib/utils";
import {
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
} from "@/shared/ui/primitives/dropdown-menu";

const MENU_PANEL = "gap-0 rounded-lg p-1";
export const MENU_ROW = "h-7 min-h-0 gap-2 rounded px-2 py-0 text-[12.5px] lg:min-h-0";
const MENU_SEP = "-mx-1 my-0.5 bg-border/70";

export function PaneMenuContent({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuContent>) {
  return <DropdownMenuContent className={cn(MENU_PANEL, className)} {...props} />;
}

export function PaneSeparator({ className }: { className?: string }) {
  return <DropdownMenuSeparator className={cn(MENU_SEP, className)} />;
}

/** A quiet group heading. */
export function PaneLabel({ children }: { children: React.ReactNode }) {
  return (
    <DropdownMenuLabel className="px-2 pb-0.5 pt-1 text-[11px] text-muted-foreground">
      {children}
    </DropdownMenuLabel>
  );
}

/** A small on/off switch for a setting row. */
export function MiniSwitch({ on }: { on: boolean }) {
  return (
    <span
      className={cn(
        "relative h-3.5 w-6 shrink-0 rounded-full transition-colors duration-150",
        on ? "bg-foreground" : "bg-muted-foreground/30",
      )}
      aria-hidden
    >
      <span
        className={cn(
          "absolute top-0.5 size-2.5 rounded-full bg-background transition-[inset-inline-start] duration-150",
          on ? "start-3" : "start-0.5",
        )}
      />
    </span>
  );
}
