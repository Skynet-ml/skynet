"use client";

import * as React from "react";
import { X } from "@/shared/ui/icons";
import { Dialog as DialogPrimitive } from "radix-ui";

import { cn } from "@/shared/lib/utils";
import { Button } from "./button";
import { msg } from "@/shared/lib/messages";

function Dialog({ ...props }: React.ComponentProps<typeof DialogPrimitive.Root>) {
  return <DialogPrimitive.Root data-slot="dialog" {...props} />;
}

function DialogPortal({ ...props }: React.ComponentProps<typeof DialogPrimitive.Portal>) {
  return <DialogPrimitive.Portal data-slot="dialog-portal" {...props} />;
}

function DialogOverlay({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
        "fixed inset-0 bg-[rgb(28_22_18/0.32)] backdrop-blur-[3px] dark:bg-black/55 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:duration-150 data-[state=closed]:ease-in data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:duration-200 data-[state=open]:ease-out",
        className,
      )}
      style={{ zIndex: 50 }}
      {...props}
    />
  );
}

function DialogContent({
  className,
  children,
  showCloseButton = true,
  onInteractOutside,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content> & {
  showCloseButton?: boolean;
}) {
  return (
    <DialogPortal data-slot="dialog-portal">
      <DialogOverlay />
      <DialogPrimitive.Content
        data-slot="dialog-content"
        className={cn(
          // Capped to the dynamic viewport so short and mobile screens never
          // push the actions off-screen; a dialog with its own fixed header and
          // footer overrides the overflow and scrolls its middle instead.
          "max-h-[calc(100dvh-2rem)] overflow-y-auto overscroll-contain",
          "fixed grid w-full max-w-[calc(100%-2rem)] gap-5 rounded-xl border bg-background p-6 shadow-[var(--shadow-float)] outline-none sm:max-w-[min(32rem,92vw)]",
          // One open and close motion for every dialog: it rises a little and
          // settles in on an ease-out curve, and leaves faster than it came.
          "data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-[0.96] data-[state=open]:slide-in-from-bottom-2 data-[state=open]:duration-[240ms] data-[state=open]:ease-[cubic-bezier(0.16,1,0.3,1)]",
          "data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-[0.98] data-[state=closed]:duration-150 data-[state=closed]:ease-in",
          "motion-reduce:[--tw-enter-scale:1] motion-reduce:[--tw-enter-translate-y:0] motion-reduce:[--tw-exit-scale:1]",
          // The close X sits in the top end corner (top-left in RTL); the header keeps clear of it.
          showCloseButton &&
            "[&>[data-slot=dialog-header]]:pe-6 [@media(any-pointer:coarse)]:[&>[data-slot=dialog-header]]:pe-8",
          className,
        )}
        // Centred with `translate`, not `transform`: the open and close
        // keyframes animate `transform`, and would drop the centring mid-motion.
        style={{ zIndex: 50, top: "50%", left: "50%", translate: "-50% -50%" }}
        // Non-Radix portals (e.g. the excel-filter dropdown) mount on
        // document.body, so Radix sees clicks inside them as "outside" and
        // would dismiss the dialog. Portals marked modal-safe opt out.
        onInteractOutside={(event) => {
          const target = event.target as Element | null;
          if (target?.closest?.("[data-modal-safe-portal]")) {
            event.preventDefault();
            return;
          }
          onInteractOutside?.(event);
        }}
        {...props}
      >
        {children}
        {showCloseButton && (
          <DialogPrimitive.Close
            data-slot="dialog-close"
            className="close-button absolute top-4 end-4 [@media(any-pointer:coarse)]:top-2 [@media(any-pointer:coarse)]:end-2"
          >
            <X />
            <span className="sr-only">{msg("shared.dialog.close")}</span>
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPortal>
  );
}

function DialogHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="dialog-header"
      className={cn("flex flex-col gap-2 text-start", className)}
      {...props}
    />
  );
}

function DialogFooter({
  className,
  showCloseButton = false,
  children,
  ...props
}: React.ComponentProps<"div"> & {
  showCloseButton?: boolean;
}) {
  return (
    <div
      data-slot="dialog-footer"
      // The actions sit in a tinted band that bleeds to the edges of
      // DialogContent's p-6. A dialog with p-0 passes mx-0 mt-0 mb-0.
      className={cn(
        "-mx-6 -mb-6 mt-1 grid grid-cols-2 gap-1.5 border-t border-[var(--border-subtle)] bg-[var(--surface-2)] px-6 py-3.5 [&_[data-slot=button]]:min-h-[44px] [&_[data-slot=button]]:w-full [&_[data-slot=button]]:justify-center lg:[&_[data-slot=button]]:min-h-0",
        className,
      )}
      {...props}
    >
      {children}
      {showCloseButton && (
        <DialogPrimitive.Close asChild>
          <Button variant="outline">{msg("shared.dialog.close")}</Button>
        </DialogPrimitive.Close>
      )}
    </div>
  );
}

function DialogTitle({ className, ...props }: React.ComponentProps<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn("text-lg leading-snug font-semibold", className)}
      {...props}
    />
  );
}

function DialogDescription({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn("text-sm leading-relaxed text-[var(--text-2)]", className)}
      {...props}
    />
  );
}

export { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle };
