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
  uncertain?: boolean;
  serverId?: string | null;
  pulling?: boolean;
}

export interface SteerQueueSnapshot {
  maxLength?: number;
  /** A Stop or a failed turn returned unread input; the composer takes it back
   *  into its draft (`takeHeld`) so nothing is sent until the user acts. */
  held: boolean;
  /** Follow-ups waiting for the chat to go idle, sent in order. */
  queued: readonly PendingMessage[];
  /** Messages handed to the running turn that the agent has not read yet. */
  steering: readonly PendingMessage[];
  /** A turn is running and accepts steering right now. */
  canSteer: boolean;
}

/** How one steerable turn takes messages in and gives back unread ones. */
export interface SteerChannel {
  /** Apply delivery proof through the owner's normal transcript path. */
  onApplied?: (event: { ids: string[]; text: string }) => void;
  /** Posts one message into the turn; resolves its server id, or null when
   *  the turn cannot take it. */
  post: (text: string) => Promise<string | null>;
  /** Takes back explicit ids and reports messages already read by the agent. */
  withdraw: (ids: string[]) => Promise<{ ids: string[]; delivered?: string[] }>;
}

interface SteerEntry extends PendingMessage {
  discarded?: boolean;
  turn: number;
  serverId: string | null;
  posting: Promise<void>;
  /** Being deleted or edited; actions wait for its withdrawal. */
  pulling: boolean;
}

const EMPTY: SteerQueueSnapshot = { queued: [], steering: [], canSteer: false, held: false };

/**
 * One chat's queued follow-ups and in-flight steers. The owner reports when
 * the chat is busy (`setBusy`), opens each steerable turn (`openTurn`) and
 * reports applied steers; the queue sends its head through `send` whenever
 * the chat goes idle, one at a time. When a steerable turn ends, steers the
 * agent never read are withdrawn and put at the front of the queue. A Stop
 * or a failed turn holds the input instead, for the composer to take back.
 */
export class SteerQueue {
  private queued: PendingMessage[] = [];
  private steering: SteerEntry[] = [];
  private busy = false;
  private held = false;
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
  private send: (text: string) => boolean | void;
  private maxLength = 20_000;
  private pullOperations = new Map<string, Promise<boolean>>();
  private settlingOperations = new Map<string, Promise<void>>();
  private trayOrder: string[] = [];

  constructor(send: (text: string) => boolean | void = () => {}, maxLength = 20_000) {
    this.send = send;
    this.maxLength = maxLength;
    this.snapshot = { ...EMPTY, maxLength };
  }

  /** Replace what starts a follow-up's turn (the owner's latest `send`). */
  setSend(send: (text: string) => boolean | void): void {
    this.send = send;
  }

  setMaxLength(maxLength: number): void {
    if (this.maxLength === maxLength) return;
    this.maxLength = maxLength;
    this.emit();
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
   * (done, error, or stop) and then sends the queue's head. A turn that
   * `failed` holds pending input instead, so an error never starts more paid
   * turns on its own.
   */
  setBusy(busy: boolean, failed = false): void {
    if (!busy && failed && this.busy) this.hold();
    if (!busy && this.channel) {
      this.endTurn();
      return;
    }
    if (!busy && !this.busy) return;
    if (this.busy === busy) return;
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
    const operation = this.settle(entries, channel, this.postChain);
    for (const entry of entries) this.settlingOperations.set(entry.id, operation);
    const finish = () => {
      for (const entry of entries) this.settlingOperations.delete(entry.id);
      this.schedulePump();
    };
    void operation.then(finish, finish);
  }

  private async withdraw(channel: SteerChannel, entries: SteerEntry[]): Promise<SteerEntry[] | null> {
    const ids = entries.map((e) => e.serverId as string);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const result = await channel.withdraw(ids);
        const delivered = entries.filter((entry) => this.steering.includes(entry) &&
          (result.delivered?.includes(entry.serverId as string) ?? !result.ids.includes(entry.serverId as string)));
        if (delivered.length) {
          const ids = delivered.map((entry) => entry.serverId as string);
          channel.onApplied?.({ ids, text: delivered.map((entry) => entry.text).join("\n\n") });
          this.applied(ids);
        }
        return entries.filter((entry) => !delivered.includes(entry)).map((entry) => {
          if (!result.ids.includes(entry.serverId as string)) entry.uncertain = true;
          return entry;
        });
      } catch (error) {
        const status = (error as { status?: number }).status;
        const retryable = error instanceof TypeError || (error as { cause?: unknown }).cause instanceof TypeError || (status !== undefined && status >= 500);
        if (!retryable || attempt === 2) return null;
        await new Promise((resolve) => setTimeout(resolve, 50 * 2 ** attempt));
      }
    }
    return null;
  }

  private async settle(entries: SteerEntry[], channel: SteerChannel, postChain: Promise<void>): Promise<void> {
    try {
      await Promise.allSettled([...this.withdrawals, postChain]);
      await Promise.allSettled(entries.map((e) => e.posting));
      const live = entries.filter((e) => e.serverId !== null && this.steering.includes(e));
      let returned: SteerEntry[] = [];
      let unknown = false;
      if (live.length > 0) {
        const outcome = await this.withdraw(channel, live);
        returned = outcome ?? live;
        unknown = outcome === null;
      }
      returned = returned.filter((e) => this.steering.includes(e) && (!e.discarded || unknown || e.uncertain));
      this.steering = this.steering.filter((e) => !entries.includes(e));
      this.returnToQueue(returned.map(({ id, text, serverId, uncertain }) => ({ id, text, serverId, uncertain: Boolean(unknown || uncertain) })));
    } finally {
      this.settling -= 1;
      this.emit();
    }
  }

  /** The agent read these steers; their text is already in the stream. */
  applied(ids: readonly string[]): boolean {
    const fresh = ids.some((id) => !this.appliedIds.has(id));
    for (const id of ids) this.appliedIds.add(id);
    const before = this.steering.length + this.queued.length;
    const applied = this.steering.filter((e) => e.serverId !== null && this.appliedIds.has(e.serverId));
    this.steering = this.steering.filter((e) => !applied.includes(e));
    const appliedIds = new Set(ids);
    this.queued = this.queued.filter((item) => !item.serverId || !appliedIds.has(item.serverId));
    if (this.steering.length + this.queued.length !== before) this.emit();
    return fresh;
  }

  /** Deliver `text` into the running turn, or queue it when none can take it. */
  steer(text: string): void {
    const trimmed = text.trim();
    if (!trimmed) return;
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
    const queuedIndex = this.trayOrder.findIndex((id) => this.queued.some((item) => item.id === id));
    this.trayOrder.splice(queuedIndex < 0 ? this.trayOrder.length : queuedIndex, 0, entry.id);
    this.post(entry, channel);
    this.steering = [...this.steering, entry];
    this.emit();
  }

  private post(entry: SteerEntry, channel: SteerChannel, prior?: Promise<void>): void {
    const fallBack = (uncertain = false) => {
      if (!this.steering.includes(entry)) return;
      this.steering = this.steering.filter((e) => e !== entry);
      if (!entry.discarded || uncertain) this.returnToQueue([{ id: entry.id, text: entry.text, uncertain }]);
      this.emit();
      this.schedulePump();
    };
    entry.posting = (prior ?? this.postChain).catch(() => {}).then(() => channel.post(entry.text)).then(
        (serverId) => {
          if (!serverId) {
            fallBack();
            return;
          }
          entry.serverId = serverId;
          if (this.appliedIds.has(serverId)) this.applied([serverId]);
        },
        (error: unknown) => {
          const status = (error as { status?: number }).status;
          fallBack(!(status !== undefined && status >= 400 && status < 500));
        },
    );
    if (prior === undefined) this.postChain = entry.posting;
  }

  /**
   * Take one steer back from the running turn before the agent reads it.
   * Resolves true when it came back; false when the agent already read it
   * (it then shows in the transcript) or its turn has ended.
   */
  private async pull(entry: SteerEntry): Promise<boolean> {
    entry.pulling = true;
    this.emit();
    const pending = [...this.withdrawals];
    const operation = Promise.allSettled(pending).then(() => this.pullEntry(entry));
    this.pullOperations.set(entry.id, operation);
    this.withdrawals.add(operation);
    try {
      return await operation;
    } finally {
      entry.pulling = false;
      this.withdrawals.delete(operation);
      this.pullOperations.delete(entry.id);
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
          this.returnToQueue([{ id: entry.id, text: entry.text, serverId: entry.serverId, uncertain: true }]);
        }
      } else {
        returned = back.includes(entry) && this.steering.includes(entry) && !entry.uncertain;
        if (entry.uncertain && this.steering.includes(entry)) {
          this.returnToQueue([{ id: entry.id, text: entry.text, serverId: entry.serverId, uncertain: true }]);
        }
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
    const settling = this.settlingOperations.get(id);
    if (settling) {
      void settling.then(() => {
        if (!this.queued.find((item) => item.id === id)?.uncertain) this.take(id);
      });
      return;
    }
    const entry = this.steering.find((e) => e.id === id);
    if (entry) {
      entry.discarded = true;
      void (this.pullOperations.get(entry.id) ?? this.pull(entry));
    }
  }

  /** Pull a pending message out for editing; resolves its text, or null when
   *  the agent already read it. */
  async edit(id: string): Promise<string | null> {
    const queued = this.take(id);
    if (queued !== null) return queued;
    const settling = this.settlingOperations.get(id);
    if (settling) {
      await settling;
      if (this.queued.find((item) => item.id === id)?.uncertain) return null;
      return this.take(id);
    }
    const entry = this.steering.find((e) => e.id === id);
    if (!entry) return null;
    if (entry.pulling) {
      await this.pullOperations.get(entry.id);
      return this.edit(id);
    }
    return (await this.pull(entry)) ? entry.text : null;
  }

  /** Put the queued follow-ups in the order of `ids`. */
  reorderQueued(ids: readonly string[]): void {
    const byId = new Map(this.queued.map((q) => [q.id, q]));
    const next = ids.map((id) => byId.get(id)).filter((q): q is PendingMessage => q !== undefined);
    if (next.length !== this.queued.length) return;
    this.queued = next;
    let index = 0;
    this.trayOrder = this.trayOrder.map((id) => byId.has(id) ? ids[index++]! : id);
    this.emit();
  }

  /** Hold `text` until the chat is idle, then send it as its own turn. */
  enqueue(text: string): void {
    const trimmed = text.trim();
    if (!trimmed) return;
    // A Stop held an earlier tray; once that tray is gone, new input is a new intent.
    if (this.held && !this.holdsInput()) this.held = false;
    const item = { id: this.makeId(), text: trimmed };
    this.queued = [...this.queued, item];
    this.trayOrder.push(item.id);
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
    const item = this.queued.find((entry) => entry.id === id);
    if (!item || item.uncertain || item.text.length > this.maxLength) return;
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
    this.held = false;
    this.trayOrder = [];
    this.emit();
  }

  /**
   * Hold pending input after a user Stop, an edit or a failed turn, including
   * steers still being withdrawn. Nothing is sent until the composer takes
   * the input back into its draft and the user sends it again.
   */
  hold(): void {
    // An empty tray has nothing to hold; holding it would hold later Tab input.
    if (this.holdsInput()) this.held = true;
    this.emit();
  }

  /**
   * Take the held input back for the composer's draft, in tray order and
   * joined by blank lines. Null while steers are still being withdrawn, so
   * the text never comes back out of order. Messages whose delivery is
   * unknown stay in the tray: the agent may already have read them.
   */
  takeHeld(): string | null {
    if (!this.held || this.settling > 0 || this.withdrawals.size > 0) return null;
    const taken = this.queued.filter((item) => !item.uncertain);
    this.queued = this.queued.filter((item) => item.uncertain);
    this.held = false;
    this.emit();
    return taken.length ? taken.map((item) => item.text).join("\n\n") : null;
  }

  private holdsInput(): boolean {
    return this.hasPending() || this.settling > 0 || this.withdrawals.size > 0;
  }

  private makeId(): string {
    this.nextId += 1;
    return `pending-${this.nextId}`;
  }

  private returnToQueue(entries: PendingMessage[]): void {
    const combined = [...entries, ...this.queued];
    const returned = combined.filter((entry) => this.trayOrder.includes(entry.id));
    returned.sort((a, b) => this.trayOrder.indexOf(a.id) - this.trayOrder.indexOf(b.id));
    this.queued = [...returned, ...combined.filter((entry) => !this.trayOrder.includes(entry.id))];
  }

  private emit(): void {
    const liveIds = new Set([...this.queued, ...this.steering].map((entry) => entry.id));
    this.trayOrder = this.trayOrder.filter((id) => liveIds.has(id));
    this.snapshot = {
      maxLength: this.maxLength,
      queued: this.queued,
      steering: this.steering.map(({ id, text, pulling }) => ({ id, text, pulling })),
      canSteer: this.busy && this.channel !== null,
      held: this.held,
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
    if (this.busy || this.settling > 0 || this.held) return;
    const head = this.queued.find((item) => !item.uncertain);
    if (!head) return;
    if (head.text.length > this.maxLength) {
      this.hold();
      return;
    }
    const queued = this.queued;
    const order = this.trayOrder;
    this.queued = this.queued.filter((item) => item !== head);
    this.emit();
    if (!this.startSend(head.text)) {
      this.queued = queued;
      this.trayOrder = order;
      this.hold();
    }
  }

  private startSend(text: string): boolean {
    this.busy = true;
    if (this.send(text) !== false) return true;
    this.busy = false;
    return false;
  }
}

/** The mid-turn surface a chat hands to its composer. */
export interface MidTurnQueue extends SteerQueueSnapshot {
  takeHeld: () => string | null;
  steer: (text: string) => void;
  queue: (text: string) => void;
  promote: (id: string) => void;
  /** Pulls a queued or unread steered message out; resolves its text, or
   *  null when the agent already read it. */
  edit: (id: string) => Promise<string | null>;
  remove: (id: string) => void;
  reorderQueued: (ids: readonly string[]) => void;
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
    takeHeld: () => getStore()?.takeHeld() ?? null,
    steer: (text) => getStore()?.steer(text),
    queue: (text) => getStore()?.enqueue(text),
    promote: (id) => getStore()?.promote(id),
    edit: async (id) => (await getStore()?.edit(id)) ?? null,
    remove: (id) => getStore()?.remove(id),
    reorderQueued: (ids) => getStore()?.reorderQueued(ids),
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
