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
import { Button } from "@/shared/ui/primitives/button";

import type { PendingMessage } from "./steer-queue";

interface PendingMessagesProps {
  paused: boolean;
  streaming: boolean;
  onSendNow: () => void;
  onStopAndSendNow?: () => void;
  queued: readonly PendingMessage[];
  steering: readonly PendingMessage[];
  /** Queued follow-ups can be moved into the running turn right now. */
  canPromote: boolean;
  onPromote: (id: string) => void;
  onEdit: (id: string) => void;
  onRemove: (id: string) => void;
  onReorderQueued: (ids: string[]) => void;
  onReorderSteering: (ids: string[]) => void;
}

const iconButtonClass = cn(
  "inline-flex size-7 shrink-0 items-center justify-center rounded-md text-muted-foreground",
  "transition-colors hover:bg-accent/60 hover:text-foreground",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
);

function PendingRow({
  item,
  steering,
  canPromote,
  onPromote,
  onEdit,
  onRemove,
  onDragEnd,
}: {
  item: PendingMessage;
  steering: boolean;
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
      className="group relative flex h-9 items-center gap-1.5 rounded-lg ps-1 pe-1 hover:bg-accent/30"
    >
      <button
        type="button"
        aria-label={msg("agent.pending.drag")}
        onPointerDown={(e) => controls.start(e)}
        className={cn(
          "flex h-7 w-4 shrink-0 cursor-grab touch-none items-center justify-center text-muted-foreground",
          "active:cursor-grabbing",
        )}
      >
        <DotsSixVertical className="size-3.5" />
      </button>
      {steering ? (
        <TooltipButton tooltip={msg("agent.pending.steering")} side="top">
          <span className="inline-flex shrink-0 text-muted-foreground">
            <ArrowBendDownRight className="size-4 rtl:-scale-x-100" />
          </span>
        </TooltipButton>
      ) : (
        <TreeView className="size-4 shrink-0 text-muted-foreground rtl:-scale-x-100" />
      )}
      <p className="min-w-0 flex-1 truncate text-sm" title={previewText(item.text)}>
        {previewText(item.text)}
      </p>
      {steering ? (
        <span className="shrink-0 px-1.5 text-xs text-muted-foreground">
          {msg("agent.pending.steering_badge")}
        </span>
      ) : (
        canPromote && (
          <button
            type="button"
            onClick={() => onPromote(item.id)}
            className={cn(
              "inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2 text-sm text-muted-foreground",
              "transition-colors hover:bg-accent/60 hover:text-foreground",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
            )}
          >
            <ArrowBendDownRight className="size-3.5 rtl:-scale-x-100" />
            {msg("agent.pending.steer")}
          </button>
        )
      )}
      <button
        type="button"
        onClick={() => onRemove(item.id)}
        aria-label={removeLabel}
        className={iconButtonClass}
      >
        <Trash className="size-4" />
      </button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button type="button" aria-label={moreLabel} className={iconButtonClass}>
            <DotsThree className="size-4" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" side="top">
          <DropdownMenuItem onSelect={() => onEdit(item.id)}>
            <PencilSimple className="size-4" />
            {msg("agent.pending.edit")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </Reorder.Item>
  );
}

/**
 * One section of the tray, reordered locally while a row is dragged and
 * committed once on drop: reordering steers takes them back from the turn
 * and posts them again, which should not happen at every pixel.
 */
function PendingSection({
  items,
  steering,
  onReorder,
  rowProps,
}: {
  items: readonly PendingMessage[];
  steering: boolean;
  onReorder: (ids: string[]) => void;
  rowProps: Pick<
    React.ComponentProps<typeof PendingRow>,
    "canPromote" | "onPromote" | "onEdit" | "onRemove"
  >;
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
          <PendingRow key={id} item={item} steering={steering} onDragEnd={commit} {...rowProps} />
        ) : null;
      })}
    </Reorder.Group>
  );
}

/**
 * The tray of messages sent while the agent was still working, attached to
 * the top of the composer: steers the agent will read at its next step, then
 * follow-ups queued to run as their own turns once it is done. Rows can be
 * dragged to reorder, steered, deleted or pulled back for editing.
 */
export function PendingMessages({
  paused,
  streaming,
  onSendNow,
  onStopAndSendNow,
  queued,
  steering,
  canPromote,
  onPromote,
  onEdit,
  onRemove,
  onReorderQueued,
  onReorderSteering,
}: PendingMessagesProps) {
  if (queued.length === 0 && steering.length === 0) return null;
  const rowProps = { canPromote, onPromote, onEdit, onRemove };
  return (
    <div
      aria-label={msg("agent.pending.list_label")}
      role="group"
      className="mx-3 rounded-t-xl border border-b-0 border-border bg-muted/40 p-1"
    >
      {(paused || (streaming && onStopAndSendNow)) && (
        <div className="flex items-center justify-between gap-2 px-2 py-1 text-xs text-muted-foreground">
          <span>{msg(paused ? "agent.pending.paused" : "agent.pending.list_label")}</span>
          {streaming && onStopAndSendNow ? (
            <Button type="button" variant="ghost" size="sm" onClick={onStopAndSendNow}>
              {msg("agent.pending.stop_and_send_now")}
            </Button>
          ) : !streaming && (
            <Button type="button" variant="ghost" size="sm" onClick={onSendNow}>
              {msg("agent.pending.send_now")}
            </Button>
          )}
        </div>
      )}
      <PendingSection items={steering} steering onReorder={onReorderSteering} rowProps={rowProps} />
      <PendingSection
        items={queued}
        steering={false}
        onReorder={onReorderQueued}
        rowProps={rowProps}
      />
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
