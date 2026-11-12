import type { ReactNode } from "react";
import { cn } from "@/shared/lib/utils";

type ColumnRoleTone = "input" | "output" | "ignore";

const ROLE_STYLES: Record<ColumnRoleTone, string> = {
  input: "bg-primary-fill text-primary-foreground",
  output: "bg-primary-fill text-primary-foreground",
  ignore: "bg-muted text-muted-foreground",
};

/** Dataset column role label (input / output / ignore). */
export function RolePill({
  role,
  className,
  children,
}: {
  role: ColumnRoleTone;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "shrink-0 rounded-md px-2 py-0.5 text-[0.625rem] font-semibold uppercase tracking-wide",
        ROLE_STYLES[role],
        className,
      )}
    >
      {children}
    </span>
  );
}
