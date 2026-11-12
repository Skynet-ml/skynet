"use client";

import { type Brand, Curl, DSPy, Go, JavaScript, Python } from "@/shared/ui/brand-marks";
import { Brain, CaretDown, Check } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";
import { formatMsg } from "@/shared/lib/messages";
import { effortLabel } from "@/shared/lib/model-efforts";

const LANG_MARKS: Partial<Record<string, Brand>> = {
  curl: Curl,
  python: Python,
  javascript: JavaScript,
  go: Go,
  dspy: DSPy,
};

export function LangPicker<T extends string>({
  value,
  onChange,
  labels,
}: {
  value: T;
  onChange: (v: T) => void;
  labels: Record<T, string>;
}) {
  const keys = Object.keys(labels) as T[];
  const Mark = LANG_MARKS[value]?.Mark;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="xs"
          className="group -ms-1.5 gap-1.5 px-1.5 text-xs font-medium tracking-normal text-foreground data-[state=open]:bg-accent [&_svg:not([class*='size-'])]:size-3.5"
        >
          {Mark && <Mark size={14} />}
          {labels[value]}
          <CaretDown
            className="size-3 text-muted-foreground transition-transform duration-150 group-data-[state=open]:rotate-180"
            aria-hidden="true"
          />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-44">
        {keys.map((k) => {
          const ItemMark = LANG_MARKS[k]?.Mark;
          return (
            <DropdownMenuItem key={k} onSelect={() => onChange(k)} className="gap-2.5">
              <span className="flex size-5 shrink-0 items-center justify-center">
                {ItemMark && <ItemMark size={16} />}
              </span>
              <span className="flex-1">{labels[k]}</span>
              {k === value && <Check className="size-4 text-primary" aria-hidden="true" />}
            </DropdownMenuItem>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
function reasoningEffortLabel(value: string | null | undefined): string | null {
  if (!value) return null;
  return effortLabel(value.toLowerCase());
}

export function ReasoningPill({
  value,
  size = "xs",
}: {
  value: string | null | undefined;
  size?: "xs" | "sm";
}) {
  const label = reasoningEffortLabel(value);
  if (!label) return null;
  const sizing =
    size === "sm" ? "gap-1 px-1.5 py-0.5 text-[10px]" : "gap-0.5 px-1 py-0.5 text-[9px]";
  const iconSize = size === "sm" ? "size-3" : "size-2.5";
  return (
    <span
      className={`shrink-0 inline-flex items-center rounded bg-muted/50 font-semibold text-muted-foreground/80 ${sizing}`}
      title={formatMsg("optimizations.reasoning_effort.tooltip", { label })}
    >
      <Brain className={iconSize} />
      {label}
    </span>
  );
}
