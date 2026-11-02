"use client";

import * as React from "react";
import { DropdownMenu as DropdownMenuPrimitive } from "radix-ui";

import { cn } from "@/shared/lib/utils";

// Shared panel chrome for the root content and submenus, in the BoardUI
// style (SKY-263): a card surface with a hairline border, inset rounded rows,
// and a short scale-and-blur entrance (`.bui-menu` in boardui.css).
const panelClass = cn(
  "bui-menu origin-(--radix-dropdown-menu-content-transform-origin)",
  "flex flex-col gap-1 rounded-2xl border border-border bg-card p-2.5 shadow-[var(--shadow-float)] [&>*]:shrink-0",
  "outline-none",
  // Long menus scroll inside the space Radix measured instead of running off-screen.
  "max-h-(--radix-dropdown-menu-content-available-height) overflow-y-auto overscroll-contain",
);

const rowClass = cn(
  "flex min-h-[44px] w-full cursor-pointer select-none items-center gap-2 rounded-[10px] p-2 text-start text-sm text-foreground",
  "outline-none transition-colors data-[highlighted]:bg-accent lg:min-h-0 any-pointer-coarse:min-h-[44px]",
  "data-[disabled]:pointer-events-none data-[disabled]:opacity-50",
);

function DropdownMenu({ ...props }: React.ComponentProps<typeof DropdownMenuPrimitive.Root>) {
  return <DropdownMenuPrimitive.Root data-slot="dropdown-menu" {...props} />;
}

function DropdownMenuTrigger({
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Trigger>) {
  return <DropdownMenuPrimitive.Trigger data-slot="dropdown-menu-trigger" {...props} />;
}

function DropdownMenuContent({
  className,
  sideOffset = 6,
  align = "center",
  collisionPadding = 8,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Content>) {
  return (
    <DropdownMenuPrimitive.Portal>
      <DropdownMenuPrimitive.Content
        data-slot="dropdown-menu-content"
        sideOffset={sideOffset}
        align={align}
        collisionPadding={collisionPadding}
        className={cn(panelClass, "max-w-[calc(100vw-1rem)]", className)}
        style={{ zIndex: 50 }}
        {...props}
      />
    </DropdownMenuPrimitive.Portal>
  );
}

function DropdownMenuItem({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Item>) {
  return (
    <DropdownMenuPrimitive.Item
      data-slot="dropdown-menu-item"
      className={cn(rowClass, className)}
      {...props}
    />
  );
}

function DropdownMenuLabel({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Label>) {
  return (
    <DropdownMenuPrimitive.Label
      data-slot="dropdown-menu-label"
      className={cn(
        "px-2 pb-0.5 pt-1 text-xs font-medium text-muted-foreground",
        className,
      )}
      {...props}
    />
  );
}

function DropdownMenuSeparator({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.Separator>) {
  return (
    <DropdownMenuPrimitive.Separator
      data-slot="dropdown-menu-separator"
      className={cn("-mx-2.5 my-1.5 h-px bg-border", className)}
      {...props}
    />
  );
}

function DropdownMenuSub({ ...props }: React.ComponentProps<typeof DropdownMenuPrimitive.Sub>) {
  return <DropdownMenuPrimitive.Sub data-slot="dropdown-menu-sub" {...props} />;
}

function DropdownMenuSubTrigger({
  className,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.SubTrigger>) {
  return (
    <DropdownMenuPrimitive.SubTrigger
      data-slot="dropdown-menu-sub-trigger"
      className={cn(rowClass, "data-[state=open]:bg-accent", className)}
      {...props}
    />
  );
}

function DropdownMenuSubContent({
  className,
  sideOffset = 6,
  collisionPadding = 8,
  ...props
}: React.ComponentProps<typeof DropdownMenuPrimitive.SubContent>) {
  return (
    <DropdownMenuPrimitive.Portal>
      <DropdownMenuPrimitive.SubContent
        data-slot="dropdown-menu-sub-content"
        sideOffset={sideOffset}
        collisionPadding={collisionPadding}
        className={cn(panelClass, className)}
        // Submenus only flip left/right and Radix never clamps the cross
        // axis, so on narrow viewports a fixed-width panel hangs off-screen.
        // Capping to the popper's computed available width lands the panel
        // exactly at the collision padding instead.
        style={{
          zIndex: 50,
          maxWidth: "var(--radix-dropdown-menu-content-available-width)",
        }}
        {...props}
      />
    </DropdownMenuPrimitive.Portal>
  );
}

export {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
  DropdownMenuSubContent,
};
