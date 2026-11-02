"use client";

import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { Tabs as TabsPrimitive } from "radix-ui";

import { cn } from "@/shared/lib/utils";

function Tabs({
  className,
  orientation = "horizontal",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root>) {
  return (
    <TabsPrimitive.Root
      data-slot="tabs"
      data-orientation={orientation}
      orientation={orientation}
      className={cn("group/tabs flex gap-2 data-[orientation=horizontal]:flex-col", className)}
      {...props}
    />
  );
}

const tabsListVariants = cva(
  "group/tabs-list inline-flex w-fit items-center justify-center rounded-lg border border-border/70 p-1.5 text-muted-foreground group-data-[orientation=horizontal]/tabs:h-11 group-data-[orientation=vertical]/tabs:h-fit group-data-[orientation=vertical]/tabs:flex-col data-[variant=line]:rounded-none data-[variant=line]:border-none data-[variant=line]:p-0 data-[variant=line]:shadow-none",
  {
    variants: {
      variant: {
        default: "bg-muted/60 backdrop-blur-sm",
        line: "gap-1 bg-transparent",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

// BoardUI tabs (SKY-263): a hairline baseline under the row and a 2px
// primary underline under the active tab. The underline is one element that
// slides between equal-width slots (see `underlineTabStyle`), or a motion
// layout span inside the active trigger.
const UNDERLINE_TABS_LIST_CLASS =
  "relative inline-flex h-auto w-full gap-0 rounded-none border-0 border-b border-border bg-transparent p-0 backdrop-blur-none";
const UNDERLINE_TABS_INDICATOR_CLASS =
  "pointer-events-none absolute -bottom-px z-0 h-0.5 bg-primary transition-[inset-inline-start] duration-200 ease-out motion-reduce:transition-none";
const UNDERLINE_TABS_TRIGGER_CLASS =
  "relative z-10 min-h-[44px] lg:min-h-10 rounded-none px-2.5 py-2 text-sm font-normal cursor-pointer border-none bg-transparent text-foreground/70 shadow-none transition-colors active:translate-y-0 active:scale-100 data-[state=active]:bg-transparent data-[state=active]:font-medium data-[state=active]:text-primary data-[state=active]:shadow-none data-[state=active]:border-none hover:text-foreground data-[state=active]:hover:text-primary focus-visible:rounded-sm focus-visible:ring-2 focus-visible:ring-primary/45 gap-1.5";

function TabsList({
  className,
  variant = "default",
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List> & VariantProps<typeof tabsListVariants>) {
  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      data-variant={variant}
      className={cn(tabsListVariants({ variant }), className)}
      {...props}
    />
  );
}

function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
        "tabs-trigger relative inline-flex h-[calc(100%-1px)] flex-1 select-none items-center justify-center gap-1.5 rounded-md border border-transparent px-3 py-1.5 text-sm font-medium whitespace-nowrap text-foreground/50 cursor-pointer transform-gpu transition-[transform,background-color,color,border-color,box-shadow,opacity] duration-120 ease-[cubic-bezier(0.2,0.8,0.2,1)] active:translate-y-[0.5px] active:scale-[0.99] motion-reduce:transform-none motion-reduce:transition-none group-data-[orientation=vertical]/tabs:w-full group-data-[orientation=vertical]/tabs:justify-start hover:text-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-1 focus-visible:outline-ring disabled:pointer-events-none disabled:opacity-50 data-[state=active]:text-primary-foreground data-[state=active]:font-medium data-[state=active]:bg-primary-fill data-[state=active]:border-primary-fill [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
        className,
      )}
      {...props}
    />
  );
}

function TabsContent({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn("flex-1 outline-none", className)}
      style={{ flex: "1 1 0%" }}
      {...props}
    />
  );
}

export {
  Tabs,
  TabsList,
  TabsTrigger,
  TabsContent,
  UNDERLINE_TABS_LIST_CLASS,
  UNDERLINE_TABS_INDICATOR_CLASS,
  UNDERLINE_TABS_TRIGGER_CLASS,
};
