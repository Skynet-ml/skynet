"use client";

import { Children, type ReactNode } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/shared/ui/primitives/card";
import { Label } from "@/shared/ui/primitives/label";
import { HelpTip } from "@/shared/ui/help-tip";
import { cn } from "@/shared/lib/utils";
import { tip as tipText, type TooltipKey } from "@/shared/lib/tooltips";

export function StepCard({
  title,
  description,
  tip,
  trailing,
  children,
  tutorial,
}: {
  title: ReactNode;
  description?: ReactNode;
  // Guidance that used to sit in the body; hovering the title shows it.
  tip?: string;
  // Rendered at the end of the title row (mode toggles, status chips).
  trailing?: ReactNode;
  children: ReactNode;
  tutorial?: string;
}) {
  const heading = (
    <CardTitle className="text-lg">{tip ? <HelpTip text={tip}>{title}</HelpTip> : title}</CardTitle>
  );
  return (
    <Card
      className="border-border/50 bg-card/80 backdrop-blur-xl"
      data-tutorial={tutorial}
    >
      <CardHeader className="px-4 sm:px-6">
        {trailing ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            {heading}
            {trailing}
          </div>
        ) : (
          heading
        )}
        {description ? <CardDescription>{description}</CardDescription> : null}
      </CardHeader>
      {/* Positioned so an expanded textarea covers the fields, not the page. */}
      {Children.toArray(children).length > 0 && (
        <CardContent className="relative space-y-5 px-4 sm:px-6">{children}</CardContent>
      )}
    </Card>
  );
}

export function Field({
  label,
  htmlFor,
  hint,
  tip,
  trailing,
  className,
  children,
}: {
  label: ReactNode;
  htmlFor?: string;
  // Extra guidance; it joins the label tooltip rather than taking a line
  // under the control.
  hint?: string;
  // Tooltip catalog key; hovering the label explains what the field controls.
  tip?: TooltipKey;
  // Rendered at the end of the label row (status chips, version steppers).
  trailing?: ReactNode;
  // Root classes, e.g. to let the field fill a flex column.
  className?: string;
  children: ReactNode;
}) {
  const help = [tip ? tipText(tip) : null, hint].filter(Boolean).join(" ");
  const labelNode = help ? <HelpTip text={help}>{label}</HelpTip> : label;
  return (
    <div className={cn("flex flex-col gap-2", className)}>
      {trailing ? (
        <div className="flex items-center justify-between gap-2">
          <Label htmlFor={htmlFor}>{labelNode}</Label>
          <div className="flex items-center gap-2">{trailing}</div>
        </div>
      ) : (
        <Label htmlFor={htmlFor}>{labelNode}</Label>
      )}
      {children}
    </div>
  );
}

/** Collapse/expand a block with the grid-rows transition the split card uses. */
export function cnGrid(open: boolean): string {
  return cn(
    "grid transition-[grid-template-rows,opacity] duration-200 ease-out",
    open ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
  );
}
