"use client";

import * as React from "react";
import { Tooltip as TooltipPrimitive } from "radix-ui";

import { cn } from "@/shared/lib/utils";

const TooltipTriggerContext = React.createContext(false);

function TooltipProvider({
  delayDuration = 0,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return (
    <TooltipPrimitive.Provider
      data-slot="tooltip-provider"
      delayDuration={delayDuration}
      {...props}
    />
  );
}

function Tooltip({ ...props }: React.ComponentProps<typeof TooltipPrimitive.Root>) {
  return <TooltipPrimitive.Root data-slot="tooltip" {...props} />;
}

function TooltipTrigger({ ...props }: React.ComponentProps<typeof TooltipPrimitive.Trigger>) {
  return (
    <TooltipTriggerContext.Provider value>
      <TooltipPrimitive.Trigger data-slot="tooltip-trigger" {...props} />
    </TooltipTriggerContext.Provider>
  );
}

function useTooltipTriggerContext() {
  return React.useContext(TooltipTriggerContext);
}

// BoardUI tooltip (SKY-263): a light card surface with a hairline border, a
// stroked caret, and a short scale-and-blur entrance (`.bui-tooltip` in
// boardui.css). Radix rotates the caret for each side.
function TooltipContent({
  className,
  sideOffset = 10,
  collisionPadding = 8,
  children,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        sideOffset={sideOffset}
        collisionPadding={collisionPadding}
        className={cn(
          "bui-tooltip max-w-[min(240px,var(--radix-tooltip-content-available-width))] break-words",
          "w-fit origin-(--radix-tooltip-content-transform-origin) rounded-lg border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-balance text-foreground shadow-[var(--shadow-float)]",
          className,
        )}
        style={{ zIndex: 50 }}
        {...props}
      >
        {children}
        <TooltipPrimitive.Arrow asChild width={12} height={7}>
          <svg
            viewBox="0 0 12 7"
            className="overflow-visible fill-card stroke-border drop-shadow-[0_1px_1px_rgb(0_0_0/0.05)]"
            style={{ zIndex: 50, transform: "translateY(-1px)" }}
          >
            <path d="M0 0 L6 6 L12 0" />
          </svg>
        </TooltipPrimitive.Arrow>
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  );
}

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider, useTooltipTriggerContext };
