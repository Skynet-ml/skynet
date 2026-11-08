"use client";

import * as React from "react";
import { Reorder, useDragControls } from "framer-motion";
import {
  ArrowBendDownRight,
  DotsSixVertical,
  DotsThree,
  PencilSimple,
  Trash,
  TreeView,
} from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";
import { splitPastes } from "./pasted-text";
import { TooltipButton } from "@/shared/ui/tooltip-button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";
import { cn } from "@/shared/lib/utils";

import type { PendingMessage } from "./steer-queue";

interface PendingMessagesProps {
  queued: readonly PendingMessage[];
  /** Queued follow-ups can be moved into the running turn right now. */
  canPromote: boolean;
  onPromote: (id: string) => void;
  onEdit: (id: string) => void;
  onRemove: (id: string) => void;
  onReorderQueued: (ids: string[]) => void;
}

const iconButtonClass = cn(
  "inline-flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground [@media(pointer:coarse)]:min-h-[44px] [@media(pointer:coarse)]:min-w-[44px]",
  "transition-colors hover:bg-accent/60 hover:text-foreground",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
  "disabled:pointer-events-none disabled:opacity-50",
);

function IconAction({
  label,
  className,
  ...props
}: { label: string } & React.ComponentProps<"button">) {
  return (
    <TooltipButton tooltip={label} side="top">
      <button type="button" aria-label={label} className={cn(iconButtonClass, className)} {...props} />
    </TooltipButton>
  );
}

function PendingRow({
  item,
  canPromote,
  onPromote,
  onEdit,
  onRemove,
  onDragEnd,
}: {
  item: PendingMessage;
  canPromote: boolean;
  onPromote: (id: string) => void;
  onEdit: (id: string) => void;
  onRemove: (id: string) => void;
  onDragEnd: () => void;
}) {
  const controls = useDragControls();
  const removeLabel = msg("agent.pending.remove");
  const moreLabel = msg("agent.pending.more");
  return (
    <Reorder.Item
      value={item.id}
      dragListener={false}
      dragControls={controls}
      onDragEnd={onDragEnd}
      className="group relative flex min-h-9 items-center gap-1.5 rounded-lg ps-1 pe-1 hover:bg-accent/30 [@media(pointer:coarse)]:min-h-[44px]"
    >
      <TooltipButton tooltip={msg("agent.pending.drag")} side="top">
        <button
          type="button"
          aria-label={msg("agent.pending.drag")}
          onPointerDown={(e) => controls.start(e)}
          className={cn(
            "flex h-7 w-4 shrink-0 cursor-grab touch-none items-center justify-center text-muted-foreground [@media(pointer:coarse)]:min-h-[44px] [@media(pointer:coarse)]:min-w-[44px]",
            "active:cursor-grabbing",
          )}
        >
          <DotsSixVertical className="size-3.5" />
        </button>
      </TooltipButton>
      {item.uncertain ? (
        <TooltipButton tooltip={msg("agent.pending.uncertain_description")} side="top">
          <button type="button" className="shrink-0 px-1.5 text-xs text-muted-foreground max-[400px]:max-w-16 max-[400px]:truncate">{msg("agent.pending.uncertain")}</button>
        </TooltipButton>
      ) : (
        <TooltipButton tooltip={msg("agent.pending.queued")} side="top">
          <span className="inline-flex shrink-0 text-muted-foreground max-[400px]:hidden">
            <TreeView className="size-4 rtl:-scale-x-100" />
          </span>
        </TooltipButton>
      )}
      <p className="min-w-0 flex-1 truncate text-sm" title={previewText(item.text)}>
        {previewText(item.text)}
      </p>
      {canPromote && !item.uncertain && (
        <IconAction label={msg("agent.pending.steer")} onClick={() => onPromote(item.id)}>
          <ArrowBendDownRight className="size-4 rtl:-scale-x-100" />
        </IconAction>
      )}
      <IconAction
        label={removeLabel}
        onClick={() => onRemove(item.id)}
        disabled={item.pulling}
        className="max-[400px]:hidden"
      >
        <Trash className="size-4" />
      </IconAction>
      <DropdownMenu>
        <TooltipButton tooltip={moreLabel} side="top">
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              aria-label={moreLabel}
              disabled={item.pulling}
              className={iconButtonClass}
            >
              <DotsThree className="size-4" />
            </button>
          </DropdownMenuTrigger>
        </TooltipButton>
        <DropdownMenuContent align="end" side="top">
          <DropdownMenuItem onSelect={() => onEdit(item.id)} className="[@media(pointer:coarse)]:min-h-[44px]">
            <PencilSimple className="size-4" />
            {msg("agent.pending.edit")}
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => onRemove(item.id)} className="[@media(pointer:coarse)]:min-h-[44px]">
            <Trash className="size-4" />
            {removeLabel}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </Reorder.Item>
  );
}

/**
 * The queued follow-ups, reordered locally while a row is dragged and
 * committed once on drop.
 */
function PendingSection({
  items,
  onReorder,
  rowProps,
}: {
  items: readonly PendingMessage[];
  onReorder: (ids: string[]) => void;
  rowProps: Pick<React.ComponentProps<typeof PendingRow>, "canPromote" | "onPromote" | "onEdit" | "onRemove">;
}) {
  const [dragOrder, setDragOrder] = React.useState<string[] | null>(null);
  const byId = new Map(items.map((item) => [item.id, item]));
  const order =
    dragOrder && dragOrder.length === items.length && dragOrder.every((id) => byId.has(id))
      ? dragOrder
      : items.map((item) => item.id);
  const commit = () => {
    if (dragOrder && order === dragOrder && order.some((id, i) => id !== items[i]?.id)) {
      onReorder(order);
    }
    setDragOrder(null);
  };
  if (items.length === 0) return null;
  return (
    <Reorder.Group axis="y" values={order} onReorder={setDragOrder} className="flex flex-col">
      {order.map((id) => {
        const item = byId.get(id);
        return item ? (
          <PendingRow key={id} item={item} onDragEnd={commit} {...rowProps} />
        ) : null;
      })}
    </Reorder.Group>
  );
}

/**
 * The tray of follow-ups queued while the agent was still working, attached
 * to the top of the composer; each runs as its own turn once the agent is
 * done. Steers show in the chat instead. Rows can be dragged to reorder,
 * steered into the running turn, deleted or pulled back for editing.
 */
export function PendingMessages({
  queued,
  canPromote,
  onPromote,
  onEdit,
  onRemove,
  onReorderQueued,
}: PendingMessagesProps) {
  if (queued.length === 0) return null;
  const rowProps = { canPromote, onPromote, onEdit, onRemove };
  return (
    <div
      aria-label={msg("agent.pending.list_label")}
      role="group"
      className="mx-3 rounded-t-xl border border-b-0 border-border bg-muted/40 p-1"
    >
      <PendingSection items={queued} onReorder={onReorderQueued} rowProps={rowProps} />
    </div>
  );
}

/** A queued message's one-line preview, with files and pastes named instead of dumped. */
function previewText(text: string): string {
  const { text: typed, pastes, files } = splitPastes(text);
  const label = [
    ...files.map((file) => `[${file.name}]`),
    ...pastes.map(() => `[${msg("shared.agent.pasted_text")}]`),
  ].join(" ");
  return [label, typed].filter(Boolean).join(" ");
}
