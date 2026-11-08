"use client";

import * as React from "react";

import { postAgentSteer, withdrawAgentSteers } from "@/shared/lib/api";

import { midTurnOf, SteerQueue, type MidTurnQueue, type SteerChannel } from "./steer-queue";

export interface UseSteerQueueArgs {
  /** Abort the owner's active turn after pending input is held. */
  stop?: () => void;
  /** A turn is running in this chat. */
  busy: boolean;
  /** The last turn ended in an error; queued input waits for the user. */
  failed?: boolean;
  /** Start a new turn with `text`; called for each queued follow-up in turn. */
  send: (text: string) => boolean | void;
  maxLength?: number;
}

export interface SteerQueueHandle {
  stop: () => void;
  /** The store, for the chat's turn machinery (`openTurn`, `applied`). */
  store: SteerQueue;
  /** The surface the composer renders and drives. */
  midTurn: MidTurnQueue;
}

/** A fresh per-turn key for the backend's steer mailbox. */
export function newSteerKey(): string {
  return crypto.randomUUID();
}

/** The backend steer mailbox of the turn started with `steerKey`. */
export function agentSteerChannel(steerKey: string, onApplied?: SteerChannel["onApplied"]): SteerChannel {
  return {
    onApplied,
    post: (text) => postAgentSteer(steerKey, text),
    withdraw: (ids) => withdrawAgentSteers(steerKey, ids),
  };
}

/**
 * One chat's mid-turn input: queued follow-ups (sent one at a time once the
 * chat is idle) and, for a chat whose turns call `store.openTurn`, steers
 * delivered into the running turn. A chat that never opens a steerable turn
 * gets a plain queue.
 *
 * `busy` is applied after the render commits, so a queued follow-up is sent
 * against the finished turn's committed history.
 */
export function useSteerQueue({ busy, failed = false, send, stop, maxLength }: UseSteerQueueArgs): SteerQueueHandle {
  const [store] = React.useState(() => new SteerQueue(send, maxLength));
  // Before the busy effect below, so a follow-up runs through the latest send.
  React.useEffect(() => {
    store.setSend(send);
    if (maxLength !== undefined) store.setMaxLength(maxLength);
    return () => store.setSend(() => false);
  }, [store, send, maxLength]);
  React.useEffect(() => {
    store.setBusy(busy, failed);
  }, [store, busy, failed]);
  const snapshot = React.useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
  const midTurn = React.useMemo(() => midTurnOf(() => store, snapshot), [store, snapshot]);
  const stopTurn = React.useCallback(() => {
    store.hold();
    stop?.();
  }, [store, stop]);
  return { store, midTurn, stop: stopTurn };
}
