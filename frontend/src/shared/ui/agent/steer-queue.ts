/**
 * Codex-style mid-turn input for the agent chats: what the user sends while a
 * turn is still running is either steered into that turn (the agent reads it
 * at its next step) or queued as a follow-up that runs once the chat is idle.
 *
 * Kept free of runtime imports so the unit-test runner can load it.
 */

/** A message the user sent while a turn was running. */
export interface PendingMessage {
  id: string;
  text: string;
}

export interface SteerQueueSnapshot {
  paused: boolean;
  /** Follow-ups waiting for the chat to go idle, sent in order. */
  queued: readonly PendingMessage[];
  /** Messages handed to the running turn that the agent has not read yet. */
  steering: readonly PendingMessage[];
  /** A turn is running and accepts steering right now. */
  canSteer: boolean;
}

/** How one steerable turn takes messages in and gives back unread ones. */
export interface SteerChannel {
  /** Posts one message into the turn; resolves its server id, or null when
   *  the turn cannot take it. */
  post: (text: string) => Promise<string | null>;
  /** Takes back explicit ids and reports messages already read by the agent. */
  withdraw: (ids: string[]) => Promise<{ ids: string[]; delivered: string[] }>;
}

interface SteerEntry extends PendingMessage {
  turn: number;
  serverId: string | null;
  posting: Promise<void>;
  /** Being deleted or edited; hidden while its withdraw is in flight. */
  pulling: boolean;
}

const EMPTY: SteerQueueSnapshot = { queued: [], steering: [], canSteer: false, paused: false };

/**
 * One chat's queued follow-ups and in-flight steers. The owner reports when
 * the chat is busy (`setBusy`), opens each steerable turn (`openTurn`) and
 * reports applied steers; the queue sends its head through `send` whenever
 * the chat goes idle, one at a time. When a steerable turn ends, steers the
 * agent never read are withdrawn and put at the front of the queue.
 */
export class SteerQueue {
  private queued: PendingMessage[] = [];
  private steering: SteerEntry[] = [];
  private busy = false;
  private paused = false;
  private sendTogether = false;
  private settling = 0;
  private turn = 0;
  private channel: SteerChannel | null = null;
  // A steer can be applied before its POST resolves with the id.
  private appliedIds = new Set<string>();
  private snapshot: SteerQueueSnapshot = EMPTY;
  private listeners = new Set<() => void>();
  private nextId = 0;
  private pumpScheduled = false;
  // Posts go out one after another so the agent reads steers in list order.
  private postChain: Promise<void> = Promise.resolve();
  private withdrawals = new Set<Promise<unknown>>();
  private send: (text: string) => void;

  constructor(send: (text: string) => void = () => {}) {
    this.send = send;
  }

  /** Replace what starts a follow-up's turn (the owner's latest `send`). */
  setSend(send: (text: string) => void): void {
    this.send = send;
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): SteerQueueSnapshot => this.snapshot;

  /** True while anything is queued or awaiting the agent. */
  hasPending(): boolean {
    return this.queued.length > 0 || this.steering.length > 0;
  }

  /**
   * Report whether a turn is running. Going idle ends the open steerable turn
   * (done, error, or stop) and then sends the queue's head.
   */
  setBusy(busy: boolean): void {
    if (!busy && this.channel) {
      this.endTurn();
      return;
    }
    if (this.busy === busy) return;
    if (busy) this.paused = false;
    this.busy = busy;
    this.emit();
    if (!busy) this.schedulePump();
  }

  /** A steerable turn started; `channel` delivers messages into it. */
  openTurn(channel: SteerChannel): void {
    if (this.channel) this.endTurn();
    this.turn += 1;
    this.channel = channel;
    this.busy = true;
    this.paused = false;
    this.appliedIds = new Set();
    this.postChain = Promise.resolve();
    this.emit();
  }

  /**
   * The open steerable turn ended. Steers it never read are withdrawn and put
   * at the front of the queue, so they run as the next turn.
   */
  private endTurn(): void {
    const channel = this.channel;
    const turn = this.turn;
    const entries = this.steering.filter((e) => e.turn === turn);
    this.channel = null;
    this.busy = false;
    if (!channel || entries.length === 0) {
      this.emit();
      this.schedulePump();
      return;
    }
    this.settling += 1;
    this.emit();
    void this.settle(entries, channel);
  }

  private async withdraw(channel: SteerChannel, entries: SteerEntry[]): Promise<SteerEntry[] | null> {
    const ids = entries.map((e) => e.serverId as string);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const result = await channel.withdraw(ids);
        return entries.filter((e) => !result.delivered.includes(e.serverId as string));
      } catch {
        // A lost response can mean withdrawal succeeded; retry the same ids.
      }
    }
    this.paused = true;
    this.sendTogether = false;
    return null;
  }

  private async settle(entries: SteerEntry[], channel: SteerChannel): Promise<void> {
    await Promise.all([...this.withdrawals, this.postChain]);
    await Promise.all(entries.map((e) => e.posting));
    const live = entries.filter((e) => e.serverId !== null && this.steering.includes(e));
    let returned: SteerEntry[] = [];
    if (live.length > 0) {
      returned = (await this.withdraw(channel, live)) ?? live;
    }
    returned = returned.filter((e) => this.steering.includes(e));
    this.steering = this.steering.filter((e) => !entries.includes(e));
    this.queued = [...returned.map(({ id, text }) => ({ id, text })), ...this.queued];
    this.settling -= 1;
    this.emit();
    this.schedulePump();
  }

  /** The agent read these steers; their text is already in the stream. */
  applied(ids: readonly string[]): void {
    for (const id of ids) this.appliedIds.add(id);
    const before = this.steering.length;
    this.steering = this.steering.filter(
      (e) => e.serverId === null || !this.appliedIds.has(e.serverId),
    );
    if (this.steering.length !== before) this.emit();
  }

  /** Deliver `text` into the running turn, or queue it when none can take it. */
  steer(text: string): void {
    const trimmed = text.trim();
    if (!trimmed) return;
    this.paused = false;
    const channel = this.channel;
    if (!channel || !this.busy) {
      this.enqueue(trimmed);
      return;
    }
    const entry: SteerEntry = {
      id: this.makeId(),
      text: trimmed,
      turn: this.turn,
      serverId: null,
      posting: Promise.resolve(),
      pulling: false,
    };
    this.post(entry, channel);
    this.steering = [...this.steering, entry];
    this.emit();
  }

  private post(entry: SteerEntry, channel: SteerChannel): void {
    const fallBack = () => {
      if (!this.steering.includes(entry)) return;
      this.steering = this.steering.filter((e) => e !== entry);
      this.queued = [{ id: entry.id, text: entry.text }, ...this.queued];
      this.emit();
      this.schedulePump();
    };
    entry.posting = this.postChain.then(() =>
      channel.post(entry.text).then(
        (serverId) => {
          if (!serverId) {
            fallBack();
            return;
          }
          entry.serverId = serverId;
          if (this.appliedIds.has(serverId)) this.applied([serverId]);
        },
        fallBack,
      ),
    );
    this.postChain = entry.posting;
  }

  /**
   * Take one steer back from the running turn before the agent reads it.
   * Resolves true when it came back; false when the agent already read it
   * (it then shows in the transcript) or its turn has ended.
   */
  private async pull(entry: SteerEntry): Promise<boolean> {
    if (this.withdrawals.size > 0) return false;
    const operation = this.pullEntry(entry);
    this.withdrawals.add(operation);
    try {
      return await operation;
    } finally {
      this.withdrawals.delete(operation);
    }
  }

  private async pullEntry(entry: SteerEntry): Promise<boolean> {
    const channel = this.channel;
    if (!channel || entry.turn !== this.turn) return false;
    entry.pulling = true;
    this.emit();
    await entry.posting;
    let returned = false;
    if (entry.serverId !== null && this.steering.includes(entry)) {
      const back = await this.withdraw(channel, [entry]);
      if (back === null) {
        if (this.steering.includes(entry)) {
          this.queued = [...this.queued, { id: entry.id, text: entry.text }];
        }
      } else {
        returned = back.includes(entry) && this.steering.includes(entry);
      }
      this.steering = this.steering.filter((e) => e !== entry);
    }
    entry.pulling = false;
    if (returned) this.steering = this.steering.filter((e) => e !== entry);
    this.emit();
    return returned;
  }

  /** Delete a pending message: dropped from the queue, or taken back from
   *  the running turn when the agent has not read it yet. */
  remove(id: string): void {
    if (this.take(id) !== null) return;
    const entry = this.steering.find((e) => e.id === id);
    if (entry && !entry.pulling) void this.pull(entry);
  }

  /** Pull a pending message out for editing; resolves its text, or null when
   *  the agent already read it. */
  async edit(id: string): Promise<string | null> {
    const queued = this.take(id);
    if (queued !== null) return queued;
    const entry = this.steering.find((e) => e.id === id);
    if (!entry || entry.pulling) return null;
    return (await this.pull(entry)) ? entry.text : null;
  }

  /** Put the queued follow-ups in the order of `ids`. */
  reorderQueued(ids: readonly string[]): void {
    const byId = new Map(this.queued.map((q) => [q.id, q]));
    const next = ids.map((id) => byId.get(id)).filter((q): q is PendingMessage => q !== undefined);
    if (next.length !== this.queued.length) return;
    this.queued = next;
    this.emit();
  }

  /**
   * Put the steers in the order of `ids`. The turn's unread steers are taken
   * back and posted again in the new order, since the agent reads them in
   * the order they arrived.
   */
  reorderSteering(ids: readonly string[]): void {
    if (this.steering.some((e) => e.pulling) || this.withdrawals.size > 0) return;
    const byId = new Map(this.steering.map((e) => [e.id, e]));
    const next = ids.map((id) => byId.get(id)).filter((e): e is SteerEntry => e !== undefined);
    if (next.length !== this.steering.length) return;
    this.steering = next;
    this.emit();
    const channel = this.channel;
    if (!channel) return;
    const turn = this.turn;
    const prior = this.postChain;
    const repost = async () => {
      await prior;
      if (this.channel !== channel || this.turn !== turn) return;
      const live = this.steering.filter((e) => e.turn === turn && e.serverId !== null && !e.pulling);
      if (live.length === 0) return;
      const returned = await this.withdraw(channel, live);
      const back = (returned ?? live).filter((e) => this.steering.includes(e));
      this.steering = this.steering.filter((e) => !live.includes(e) || back.includes(e));
      if (returned === null || this.channel !== channel || this.turn !== turn) {
        // Unknown outcomes stay paused; a finished turn returns unread input.
        this.steering = this.steering.filter((e) => !back.includes(e));
        this.queued = [...back.map(({ id, text }) => ({ id, text })), ...this.queued];
        this.emit();
        this.schedulePump();
        return;
      }
      for (const entry of back) {
        entry.serverId = null;
        this.post(entry, channel);
      }
    };
    const operation = repost();
    this.withdrawals.add(operation);
    void operation.finally(() => this.withdrawals.delete(operation));
    this.postChain = operation;
  }

  /** Hold `text` until the chat is idle, then send it as its own turn. */
  enqueue(text: string): void {
    const trimmed = text.trim();
    if (!trimmed) return;
    this.queued = [...this.queued, { id: this.makeId(), text: trimmed }];
    this.emit();
    this.schedulePump();
  }

  /** Remove a queued follow-up and return its text (null when gone). */
  take(id: string): string | null {
    const item = this.queued.find((q) => q.id === id);
    if (!item) return null;
    this.queued = this.queued.filter((q) => q !== item);
    this.emit();
    return item.text;
  }

  /** Move a queued follow-up into the running turn. */
  promote(id: string): void {
    if (!this.channel || !this.busy) return;
    const text = this.take(id);
    if (text !== null) this.steer(text);
  }

  /** Pull the last queued follow-up back out for editing. */
  recallLast(): string | null {
    const last = this.queued[this.queued.length - 1];
    return last ? this.take(last.id) : null;
  }

  /** Drop everything (a new chat or a reset). */
  clear(): void {
    this.queued = [];
    this.steering = [];
    this.paused = false;
    this.sendTogether = false;
    this.emit();
  }

  /** Hold pending input after a user Stop, including steers still being withdrawn. */
  pause(): void {
    this.paused = true;
    this.sendTogether = false;
    this.emit();
  }

  /** Resume the head through the owner's normal turn-start path. */
  sendNow(): void {
    if (this.busy || !this.paused) return;
    this.paused = false;
    this.emit();
    this.pump();
  }

  /** Stop the active turn, then send the settled tray as one follow-up. */
  stopAndSendNow(stop: () => void): void {
    if (!this.busy || !this.hasPending()) return;
    this.pause();
    stop();
    this.sendTogether = true;
    this.schedulePump();
  }

  private makeId(): string {
    this.nextId += 1;
    return `pending-${this.nextId}`;
  }

  private emit(): void {
    this.snapshot = {
      queued: this.queued,
      steering: this.steering.filter((e) => !e.pulling).map(({ id, text }) => ({ id, text })),
      canSteer: this.busy && this.channel !== null,
      paused: this.paused,
    };
    for (const listener of this.listeners) listener();
  }

  // Deferred so the owner's turn-end handler finishes before the next turn
  // starts, and coalesced so two idle signals never send two messages.
  private schedulePump(): void {
    if (this.pumpScheduled) return;
    this.pumpScheduled = true;
    queueMicrotask(() => {
      this.pumpScheduled = false;
      this.pump();
    });
  }

  private pump(): void {
    if (this.busy || this.settling > 0 || (this.paused && !this.sendTogether)) return;
    if (this.sendTogether) {
      const text = this.queued.map((item) => item.text).join("\n\n");
      this.queued = [];
      this.sendTogether = false;
      this.paused = false;
      this.emit();
      if (text) this.send(text);
      return;
    }
    const head = this.queued[0];
    if (!head) return;
    this.queued = this.queued.slice(1);
    this.emit();
    this.send(head.text);
  }
}

/** The mid-turn surface a chat hands to its composer. */
export interface MidTurnQueue extends SteerQueueSnapshot {
  sendNow: () => void;
  stopAndSendNow: (stop: () => void) => void;
  steer: (text: string) => void;
  queue: (text: string) => void;
  promote: (id: string) => void;
  /** Pulls a queued or unread steered message out; resolves its text, or
   *  null when the agent already read it. */
  edit: (id: string) => Promise<string | null>;
  remove: (id: string) => void;
  reorderQueued: (ids: readonly string[]) => void;
  reorderSteering: (ids: readonly string[]) => void;
  recallLast: () => string | null;
}

/** Bind a snapshot to the store it came from. The store is looked up lazily so
 *  a caller may resolve it per call (e.g. the displayed session's). */
export function midTurnOf(
  getStore: () => SteerQueue | null,
  snapshot: SteerQueueSnapshot,
): MidTurnQueue {
  return {
    ...snapshot,
    sendNow: () => getStore()?.sendNow(),
    stopAndSendNow: (stop) => getStore()?.stopAndSendNow(stop),
    steer: (text) => getStore()?.steer(text),
    queue: (text) => getStore()?.enqueue(text),
    promote: (id) => getStore()?.promote(id),
    edit: async (id) => (await getStore()?.edit(id)) ?? null,
    remove: (id) => getStore()?.remove(id),
    reorderQueued: (ids) => getStore()?.reorderQueued(ids),
    reorderSteering: (ids) => getStore()?.reorderSteering(ids),
    recallLast: () => getStore()?.recallLast() ?? null,
  };
}

export const EMPTY_STEER_SNAPSHOT = EMPTY;

export type MidTurnKeyAction = "steer" | "queue" | "recall" | null;

/**
 * What a composer keystroke does to mid-turn input. While a turn runs, Enter
 * steers (or queues, in a chat that cannot steer) and Tab queues; Up in an
 * empty composer pulls the last queued follow-up back for editing.
 */
export function midTurnKeyAction(
  key: { key: string; shiftKey: boolean; altKey: boolean; ctrlKey: boolean; metaKey: boolean },
  ctx: { streaming: boolean; draft: string; canSteer: boolean; queuedCount: number },
): MidTurnKeyAction {
  if (key.altKey || key.ctrlKey || key.metaKey || key.shiftKey) return null;
  const hasDraft = ctx.draft.trim().length > 0;
  if (key.key === "Enter" && ctx.streaming && hasDraft) return ctx.canSteer ? "steer" : "queue";
  if (key.key === "Tab" && ctx.streaming && hasDraft) return "queue";
  if (key.key === "ArrowUp" && ctx.draft === "" && ctx.queuedCount > 0) return "recall";
  return null;
}

/** Append an edited follow-up to whatever is already in the draft. */
export function appendToDraft(draft: string, text: string): string {
  return draft.trim() ? `${draft.replace(/\s+$/, "")}\n${text}` : text;
}
