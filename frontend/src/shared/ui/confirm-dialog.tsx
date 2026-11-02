"use client";

import * as React from "react";
import { CircleNotch } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Dialog, DialogContent, DialogFooter } from "@/shared/ui/primitives/dialog";
import { DialogTitleRow } from "@/shared/ui/dialog-title-row";
import { cn } from "@/shared/lib/utils";

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  description?: React.ReactNode;
  /** Extra content between the header and the buttons, such as a warning row. */
  children?: React.ReactNode;
  confirmLabel: React.ReactNode;
  cancelLabel: React.ReactNode;
  onConfirm: () => void;
  /** "danger" paints the confirm button red and puts first focus on Cancel. */
  variant?: "default" | "danger";
  /** While pending, both buttons are disabled and the dialog cannot close. */
  pending?: boolean;
  confirmDisabled?: boolean;
  className?: string;
}

/**
 * The body of a confirm dialog. It must render inside a Radix `Dialog` root;
 * `ConfirmDialog` adds the root and the portal content.
 */
export function ConfirmDialogBody({
  title,
  description,
  children,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onCancel,
  variant = "default",
  pending = false,
  confirmDisabled = false,
  cancelRef,
}: Omit<ConfirmDialogProps, "open" | "onOpenChange" | "className"> & {
  onCancel: () => void;
  cancelRef?: React.Ref<HTMLButtonElement>;
}) {
  return (
    <>
      <DialogTitleRow title={title} description={description} />
      {children}
      <DialogFooter>
        <Button ref={cancelRef} variant="outline" onClick={onCancel} disabled={pending}>
          {cancelLabel}
        </Button>
        <Button
          variant={variant === "danger" ? "destructive" : "default"}
          onClick={onConfirm}
          disabled={pending || confirmDisabled}
          aria-busy={pending || undefined}
        >
          {pending ? (
            <>
              <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              {/* The spinner replaces the label, so keep the button's name for screen readers. */}
              <span className="sr-only">{confirmLabel}</span>
            </>
          ) : (
            confirmLabel
          )}
        </Button>
      </DialogFooter>
    </>
  );
}

/**
 * Wrap `onOpenChange` so the dialog cannot close while its request runs.
 *
 * A close during the request would hide its result and allow a second click.
 */
export function guardOpenChange(
  pending: boolean,
  onOpenChange: (open: boolean) => void,
): (open: boolean) => void {
  return (next) => {
    if (!next && pending) return;
    onOpenChange(next);
  };
}

/**
 * A modal that asks the user to confirm one action.
 *
 * Use it for delete, remove and disconnect prompts so every one has the same
 * size, button order, pending spinner and focus rule. Dialogs with forms or
 * custom layouts keep using the dialog primitives directly.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  className,
  pending = false,
  variant = "default",
  ...body
}: ConfirmDialogProps) {
  const cancelRef = React.useRef<HTMLButtonElement>(null);
  const requestOpenChange = guardOpenChange(pending, onOpenChange);
  return (
    <Dialog open={open} onOpenChange={requestOpenChange}>
      <DialogContent
        className={cn("w-[min(28rem,92vw)] max-w-[min(28rem,92vw)] sm:max-w-md", className)}
        onOpenAutoFocus={(event) => {
          // A destructive action must never be one Enter key away.
          if (variant !== "danger") return;
          event.preventDefault();
          cancelRef.current?.focus();
        }}
      >
        <ConfirmDialogBody
          {...body}
          variant={variant}
          pending={pending}
          onCancel={() => requestOpenChange(false)}
          cancelRef={cancelRef}
        />
      </DialogContent>
    </Dialog>
  );
}
