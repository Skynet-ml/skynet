"use client";

/**
 * Drive-style controls for an item another user shared with the caller:
 * remove it from view (with Undo), lower the caller's own role, and remove the
 * caller's own access. Every action changes only the caller's own grant.
 */

import * as React from "react";
import { Popover as PopoverPrimitive } from "radix-ui";
import { toast } from "react-toastify";
import {
  hideSharedItem,
  lowerSharedRole,
  removeSharedAccess,
  unhideSharedItem,
  type ShareRole,
  type SharedItemKind,
} from "@/shared/lib/api";
import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { canLowerToViewer } from "@/shared/lib/shared-with-me";
import { userErrorMessage } from "@/shared/lib/user-error";
import { cn } from "@/shared/lib/utils";
import {
  COMPACT_POPOVER_ICON_CLASS,
  COMPACT_POPOVER_ITEM_CLASS,
  COMPACT_POPOVER_PANEL_CLASS,
} from "@/shared/ui/compact-popover-menu";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { DotsThree, Eye, Info, MinusCircle, SignOut } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";

export interface SharedItemRef {
  kind: SharedItemKind;
  id: string;
  name: string;
  role: ShareRole | null | undefined;
}

export interface SharedItemActions {
  role: ShareRole | null | undefined;
  hide: () => void;
  lowerToViewer: () => void;
  requestRemove: () => void;
  /** The remove-access confirm dialog; render it outside any popover. */
  dialog: React.ReactNode;
}

/** State and handlers for one shared item's controls. */
export function useSharedItemActions(
  item: SharedItemRef,
  { onChanged, onRemoved }: { onChanged: () => void; onRemoved?: () => void },
): SharedItemActions {
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [pending, setPending] = React.useState(false);
  const fail = (err: unknown) => toast.error(userErrorMessage(err, msg("shared_with_me.failed")));

  const undo = async () => {
    try {
      await unhideSharedItem(item.kind, item.id);
      onChanged();
    } catch (err) {
      fail(err);
    }
  };

  const hide = async () => {
    try {
      await hideSharedItem(item.kind, item.id);
      onChanged();
      toast.success(({ closeToast }) => (
        <span className="flex items-center gap-3">
          <span className="min-w-0 flex-1">{msg("shared_with_me.hidden")}</span>
          <button
            type="button"
            onClick={() => {
              closeToast();
              void undo();
            }}
            className="shrink-0 cursor-pointer font-semibold text-primary hover:underline focus-visible:underline focus-visible:outline-none"
          >
            {msg("shared_with_me.undo")}
          </button>
        </span>
      ));
    } catch (err) {
      fail(err);
    }
  };

  const lowerToViewer = async () => {
    try {
      await lowerSharedRole(item.kind, item.id, "viewer");
      toast.success(msg("shared_with_me.made_viewer"));
      onChanged();
    } catch (err) {
      fail(err);
    }
  };

  const confirmRemove = async () => {
    if (pending) return;
    setPending(true);
    try {
      await removeSharedAccess(item.kind, item.id);
      setConfirmOpen(false);
      toast.success(msg("shared_with_me.removed"));
      onRemoved?.();
      onChanged();
    } catch (err) {
      fail(err);
    } finally {
      setPending(false);
    }
  };

  const [bodyBefore, bodyAfter = ""] = msg("shared_with_me.remove.body").split("{name}");
  const dialog = (
    <ConfirmDialog
      open={confirmOpen}
      onOpenChange={setConfirmOpen}
      variant="danger"
      title={msg("shared_with_me.remove.title")}
      description={
        <>
          {bodyBefore}
          <span className="break-words font-semibold text-foreground" dir="auto">
            {item.name}
          </span>
          {bodyAfter}
        </>
      }
      cancelLabel={msg("shared_with_me.remove.cancel")}
      confirmLabel={msg("shared_with_me.remove.confirm")}
      onConfirm={confirmRemove}
      pending={pending}
    />
  );

  return {
    role: item.role,
    hide: () => void hide(),
    lowerToViewer: () => void lowerToViewer(),
    requestRemove: () => setConfirmOpen(true),
    dialog,
  };
}

/**
 * The menu entries for a shared item, for use inside a compact popover.
 *
 * Access inherited from a shared folder has no grant of its own, so the
 * entries give way to a note that names that folder.
 */
export function SharedItemMenuItems({
  actions,
  inheritedFrom,
}: {
  actions: SharedItemActions;
  inheritedFrom?: string | null;
}) {
  const isRtl = getActiveDir() === "rtl";
  if (inheritedFrom) {
    return (
      <p className="flex items-start gap-2 px-3 py-2 text-xs text-muted-foreground">
        <Info className={cn(COMPACT_POPOVER_ICON_CLASS, "mt-px")} aria-hidden="true" />
        <span className="min-w-0 flex-1 text-start">
          {formatMsg("shared_with_me.inherited", { folder: inheritedFrom })}
        </span>
      </p>
    );
  }
  return (
    <>
      <PopoverPrimitive.Close asChild>
        <button type="button" onClick={actions.hide} className={COMPACT_POPOVER_ITEM_CLASS}>
          <MinusCircle className={COMPACT_POPOVER_ICON_CLASS} aria-hidden="true" />
          <span className="flex-1 text-start">{msg("shared_with_me.hide")}</span>
        </button>
      </PopoverPrimitive.Close>
      {canLowerToViewer(actions.role) && (
        <PopoverPrimitive.Close asChild>
          <button
            type="button"
            onClick={actions.lowerToViewer}
            className={COMPACT_POPOVER_ITEM_CLASS}
          >
            <Eye className={COMPACT_POPOVER_ICON_CLASS} aria-hidden="true" />
            <span className="flex-1 text-start">{msg("shared_with_me.make_viewer")}</span>
          </button>
        </PopoverPrimitive.Close>
      )}
      <PopoverPrimitive.Close asChild>
        <button
          type="button"
          onClick={actions.requestRemove}
          className={cn(
            COMPACT_POPOVER_ITEM_CLASS,
            "text-destructive hover:bg-destructive/10 focus-visible:bg-destructive/10",
          )}
        >
          <SignOut
            className={cn(COMPACT_POPOVER_ICON_CLASS, "text-destructive", isRtl && "-scale-x-100")}
            aria-hidden="true"
          />
          <span className="flex-1 text-start">{msg("shared_with_me.remove")}</span>
        </button>
      </PopoverPrimitive.Close>
    </>
  );
}

/** A "⋯" button with the shared-item controls, for list rows without a menu. */
export function SharedItemMenu({
  item,
  onChanged,
}: {
  item: SharedItemRef;
  onChanged: () => void;
}) {
  const actions = useSharedItemActions(item, { onChanged });
  return (
    <>
      <PopoverPrimitive.Root>
        <PopoverPrimitive.Trigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="text-muted-foreground hover:text-foreground"
            autoTooltip={false}
            aria-label={formatMsg("shared_with_me.menu", { name: item.name })}
          >
            <DotsThree className="size-4" aria-hidden="true" />
          </Button>
        </PopoverPrimitive.Trigger>
        <PopoverPrimitive.Portal>
          <PopoverPrimitive.Content
            align="end"
            side="bottom"
            sideOffset={6}
            collisionPadding={8}
            className={COMPACT_POPOVER_PANEL_CLASS}
            onClick={(e) => e.stopPropagation()}
          >
            <SharedItemMenuItems actions={actions} />
          </PopoverPrimitive.Content>
        </PopoverPrimitive.Portal>
      </PopoverPrimitive.Root>
      {actions.dialog}
    </>
  );
}
