import { SETUP_CHAT_KEY } from "@/features/agent-panel";
import type { ServerSetupDraft, ServerSetupDraftSlot, SetupDraftSaveMark } from "@/shared/lib/api";
import {
  clearSetupDraft,
  decodeSetupDraft,
  fitsKeepalive,
  readSetupDraft,
  writeSetupDraft,
  type SetupDraft,
} from "@/shared/lib/setup-draft";

import {
  WIZARD_SESSION_KEY,
  WIZARD_VIEW_KEYS,
  encodeWizardSession,
  keepSessionValue,
  readSessionValue,
  setupWorthKeeping,
} from "./session-state";
import type { WizardV2State } from "./wizard-v2";

/** The server's draft calls; the app passes the API client, tests pass fakes. */
export interface SetupDraftServer {
  get(signal?: AbortSignal): Promise<ServerSetupDraftSlot>;
  /** Save the draft when ``baseRevision`` is still the slot's revision. */
  put(
    draft: ServerSetupDraft,
    baseRevision: number,
    keepalive?: boolean,
    signal?: AbortSignal,
    mark?: SetupDraftSaveMark,
  ): Promise<ServerSetupDraftSlot>;
  /** Delete the draft when ``revision`` is still the slot's revision. */
  delete(revision: number, signal?: AbortSignal): Promise<ServerSetupDraftSlot>;
}

/** How long the wizard waits after a change before it saves the draft, in ms. */
export const SETUP_DRAFT_SAVE_DELAY_MS = 1500;
/** How long /submit waits for each server call of a load before it uses this browser's copy, in ms. */
export const SETUP_DRAFT_LOAD_TIMEOUT_MS = 3000;
/** How long a save or a delete may take before the next draft call goes ahead, in ms. */
export const SETUP_DRAFT_WRITE_TIMEOUT_MS = 10_000;
// A delete the server missed after a submit or Start new, with the slot
// revision it read; the next load sends it again, so the setup does not come
// back on another device. The server keeps a draft saved after that read, so
// a late retry is safe.
export const SETUP_DRAFT_DELETE_PENDING_KEY = "skynet.wizard.draft-delete.v1";
/**
 * The tab's setup as a left setup, or null when it holds nothing worth
 * offering back.
 *
 * Args:
 *   account: The signed-in account.
 *   state: The wizard's state.
 *
 * Returns:
 *   The draft, or null.
 */
export function setupDraftOf(account: string, state: WizardV2State): SetupDraft | null {
  if (!setupWorthKeeping(state)) return null;
  const values: Record<string, string> = {
    [WIZARD_SESSION_KEY]: encodeWizardSession(account, state),
  };
  for (const key of [...WIZARD_VIEW_KEYS, SETUP_CHAT_KEY]) {
    const value = readSessionValue(key);
    if (value !== null) values[key] = value;
  }
  return { account, leftAt: Date.now(), values };
}

/**
 * Split a left setup into the server's shape: the chat as an object, and the
 * staged upload the answers use, so the server keeps that upload.
 *
 * Args:
 *   draft: The left setup.
 *
 * Returns:
 *   The body for the server.
 */
export function toServerDraft(draft: SetupDraft): ServerSetupDraft {
  const { [SETUP_CHAT_KEY]: chatText, ...values } = draft.values;
  return {
    left_at: Math.max(0, Math.floor(draft.leftAt)),
    values,
    chat: parseObject(chatText),
    staged_dataset_id: stagedDatasetIdOf(values[WIZARD_SESSION_KEY]),
  };
}

/**
 * Join the server's draft back into the tab's keys.
 *
 * Args:
 *   account: The signed-in account the server answered for.
 *   draft: The server's draft.
 *
 * Returns:
 *   The left setup.
 */
export function fromServerDraft(account: string, draft: ServerSetupDraft): SetupDraft {
  const values = { ...draft.values };
  if (draft.chat) values[SETUP_CHAT_KEY] = JSON.stringify(draft.chat);
  return { account, leftAt: draft.left_at, values };
}

function parseObject(text: string | undefined): Record<string, unknown> | null {
  if (!text) return null;
  try {
    const value: unknown = JSON.parse(text);
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

// The same rule as the run payload: rows typed into the setup replace the upload.
function stagedDatasetIdOf(sessionText: string | undefined): string | null {
  const session = parseObject(sessionText);
  const inputs = (
    session?.state as { inputs?: { cases?: unknown; stagedDatasetId?: unknown } } | undefined
  )?.inputs;
  if (!Array.isArray(inputs?.cases) || inputs.cases.length > 0) return null;
  const id = inputs.stagedDatasetId;
  return typeof id === "string" && id ? id : null;
}

/** Keep a left setup in this browser; the chat goes first when storage is full. */
export function keepLocalSetupDraft(draft: SetupDraft): void {
  if (writeSetupDraft(draft) || !(SETUP_CHAT_KEY in draft.values)) return;
  const { [SETUP_CHAT_KEY]: _, ...values } = draft.values;
  writeSetupDraft({ ...draft, values });
}

// The slot revision this page last got from the server, by account.
let known: { account: string; revision: number } | null = null;

function knownRevision(account: string): number | null {
  return known?.account === account ? known.revision : null;
}

// The account whose server draft is not the setup this page shows. A delete
// from this page then names revision 0, which no stored setup has, so it
// cannot remove the other setup.
let foreign: string | null = null;

function deleteRevision(account: string): number | null {
  return foreign === account ? 0 : knownRevision(account);
}

// The account whose slot this page read once; known can turn null after that.
let readSlot: string | null = null;

function learn(account: string, slot: ServerSetupDraftSlot): ServerSetupDraftSlot {
  known = { account, revision: slot.revision };
  readSlot = account;
  return slot;
}

/** This browser's copy for ``account``, or null; another account's copy stays where it is. */
function localDraftOf(account: string): SetupDraft | null {
  const draft = decodeSetupDraft(readSetupDraft());
  return draft?.account === account ? draft : null;
}

function clearLocalDraftOf(account: string): void {
  if (localDraftOf(account)) clearSetupDraft();
}

function deletePendingKey(account: string): string {
  return `${SETUP_DRAFT_DELETE_PENDING_KEY}:${account}`;
}

function deletePending(account: string): number | null {
  try {
    const value = window.localStorage.getItem(deletePendingKey(account));
    return value === null ? null : Number(value) || 0;
  } catch {
    return null;
  }
}

function markDeletePending(account: string, leftAt: number | null): void {
  try {
    if (leftAt !== null) window.localStorage.setItem(deletePendingKey(account), String(leftAt));
    else window.localStorage.removeItem(deletePendingKey(account));
  } catch {}
}

/** Fail and cancel a server call that takes longer than ``ms``, so no caller waits on it for ever. */
function withTimeout<T>(start: (signal: AbortSignal) => Promise<T>, ms: number): Promise<T> {
  const controller = new AbortController();
  const call = start(controller.signal);
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      controller.abort();
      reject(new Error("setup draft call timed out"));
    }, ms);
    call.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err: unknown) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

// One tab sends its draft calls in order, so a save started before a delete
// cannot land after it, and the last save is the one the server keeps.
let pending: Promise<unknown> = Promise.resolve();
function queued<T>(work: () => Promise<T>): Promise<T> {
  const run = pending.then(work, work);
  pending = run.catch(() => undefined);
  return run;
}

// A call the page may not outlive starts at once; later calls still wait for it.
function startedNow<T>(work: () => Promise<T>): Promise<T> {
  const run = work();
  const before = pending;
  pending = Promise.all([before, run.catch(() => undefined)]);
  return run;
}

// The server rejects a draft (413) only when its answers alone are over the
// cap, so the same answers are not sent again; the browser copy keeps them.
let rejectedValues: string | null = null;
// The draft this page last read from the server or the server last took from
// it, without its time. It is not sent again; a setup that differs holds an
// edit the server never took.
let takenBody: string | null = null;
// The tab keeps takenBody too: a reload reopens the tab's own setup, and that
// setup holds no edit when it matches what this tab last synced.
const TAKEN_BODY_KEY = "skynet.setup-draft.taken.v1";
// Counts deletes; a save that a delete followed must not put the setup back.
let deletes = 0;
// This page's id and count of saves; the server keeps the latest count of each
// page, so a load can tell whether the save of a browser copy landed.
let writer = newWriter();
let saveCount = 0;

function newWriter(): string {
  return (
    globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
}

/** Forget what this tab sent and read, as a new page does. */
export function resetSetupDraftSyncForTests(): void {
  rejectedValues = null;
  takenBody = null;
  known = null;
  foreign = null;
  readSlot = null;
  writer = newWriter();
  saveCount = 0;
}

// Postgres JSONB reorders object keys, so a draft read back from the server
// must compare equal to the same draft built in this page.
function sortedKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortedKeys);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.keys(value)
      .sort()
      .map((key) => [key, sortedKeys((value as Record<string, unknown>)[key])]),
  );
}

function bodyKey(account: string, body: ServerSetupDraft): string {
  return `${account}\n${JSON.stringify(sortedKeys({ ...body, left_at: 0 }))}`;
}

function take(key: string | null): void {
  takenBody = key;
  if (key !== null) {
    keepSessionValue(TAKEN_BODY_KEY, key);
    return;
  }
  try {
    window.sessionStorage.removeItem(TAKEN_BODY_KEY);
  } catch {}
}

/**
 * Save a left setup on the server. When the server call fails, the copy
 * stays in this browser and the next load sends it.
 *
 * Args:
 *   draft: The left setup.
 *   server: The server's draft calls.
 *   keepalive: True when the page may close before the call ends.
 *
 * Returns:
 *   True when the server took the draft or holds it already.
 */
export function saveSetupDraft(
  draft: SetupDraft,
  server: SetupDraftServer,
  keepalive = false,
): Promise<boolean> {
  const { account } = draft;
  const body = toServerDraft(draft);
  // A body too large for keepalive goes as a normal call, which the page may not outlive.
  const outlivesPage = keepalive && fitsKeepalive(JSON.stringify(body));
  const values = JSON.stringify(body.values);
  const unchanged = bodyKey(account, body);
  const deletesBefore = deletes;
  const save = { writer, seq: (saveCount += 1) };
  const send = (base: number) =>
    withTimeout(
      (signal) => server.put(body, base, outlivesPage, signal, save),
      SETUP_DRAFT_WRITE_TIMEOUT_MS,
    ).then((slot) => learn(account, slot));
  // A page that never read the slot keeps no base, so its copy differs from one built on revision 0.
  const keepCopy = () => {
    const base = knownRevision(account) ?? (readSlot === account ? 0 : null);
    keepLocalSetupDraft(
      base === null ? { ...draft, save } : { ...draft, save, baseRevision: base },
    );
  };
  // A stale save goes once more only on top of this page's own setup, or of
  // the delete of the revision this page knew; a newer setup from another
  // device stays.
  const ownSlot = (slot: ServerSetupDraftSlot, read: number | null) => {
    // A page whose load failed never learned the revision; an empty slot is still safe to fill.
    if (!slot.draft)
      return slot.revision === (read ?? 0) + 1 || (read === null && foreign !== account);
    // An earlier save of this page that landed first is this page's setup too.
    if (slot.writer === writer) return true;
    const left = fromServerDraft(account, slot.draft);
    return (
      bodyKey(account, toServerDraft(left)) === takenBody ||
      left.values[WIZARD_SESSION_KEY] === draft.values[WIZARD_SESSION_KEY]
    );
  };
  const work = async () => {
    // A setup the server took or gave is not sent again, not even on leave: a
    // save from an idle tab would raise the revision, and a delete another tab
    // sends from the old revision would then miss.
    if (unchanged === takenBody) {
      clearLocalDraftOf(account);
      return true;
    }
    // The page may close before the answer, so a leave save keeps its copy first.
    if (keepalive) keepCopy();
    try {
      if (values === rejectedValues) throw new Error("setup draft too large");
      const read = known;
      let slot = await send(knownRevision(account) ?? 0);
      // Another tab or device wrote or deleted the slot since this page read
      // it. This setup holds an edit the server never took, so it goes once
      // more on top of the same setup.
      if (!slot.changed && deletes === deletesBefore) {
        // A later save of this page holds the slot already; this older one must not replace it.
        if (slot.draft && slot.writer === writer && (slot.seq ?? 0) > save.seq) return true;
        if (!ownSlot(slot, read?.account === account ? read.revision : null)) {
          // Later saves from this page must not land on the newer setup either.
          known = read;
          foreign = account;
          throw new Error("setup draft superseded");
        }
        slot = await send(slot.revision);
      }
      if (!slot.changed) throw new Error("setup draft save refused");
    } catch (err) {
      if ((err as { status?: unknown } | null)?.status === 413) rejectedValues = values;
      // After a submit or Start new this setup is gone; a copy would bring it back.
      if (deletes === deletesBefore) keepCopy();
      return false;
    }
    rejectedValues = null;
    if (foreign === account) foreign = null;
    if (deletes === deletesBefore) take(unchanged);
    // A later delete must name the revision this save made.
    else if (deletePending(account) !== null) markDeletePending(account, knownRevision(account));
    clearLocalDraftOf(account);
    return true;
  };
  return keepalive ? startedNow(work) : queued(work);
}

/**
 * Send a delete for the slot revision this page read.
 *
 * Args:
 *   account: The signed-in account.
 *   server: The server's draft calls.
 *   revision: The revision the delete names.
 *   ms: How long the call may take.
 *   anyRevision: True when this page read no revision, so a stale delete goes once more.
 *   held: The setup this page held, without its time, or null.
 *   tab: The answers this tab holds, or null.
 *
 * Returns:
 *   True when the server answered.
 */
async function sendDelete(
  account: string,
  server: SetupDraftServer,
  revision: number,
  ms: number,
  anyRevision = false,
  held: string | null = null,
  tab: string | null = null,
): Promise<boolean> {
  let slot: ServerSetupDraftSlot;
  try {
    slot = learn(account, await withTimeout((signal) => server.delete(revision, signal), ms));
    // A write that only put the same setup back raised the revision; the
    // delete still goes. A different setup from another device stays.
    // The tab's own answers count too: a leave save can land after the page that sent it is gone.
    const left = slot.draft ? fromServerDraft(account, slot.draft) : null;
    const sameSetup =
      left !== null &&
      ((held !== null && bodyKey(account, toServerDraft(left)) === held) ||
        (tab !== null && left.values[WIZARD_SESSION_KEY] === tab));
    if (!slot.changed && (anyRevision || sameSetup)) {
      slot = learn(
        account,
        await withTimeout((signal) => server.delete(slot.revision, signal), ms),
      );
    }
  } catch {
    markDeletePending(account, revision);
    return false;
  }
  // A delete still stale means another device saved a different setup after this page read; that one stays.
  if (deletePending(account) === revision) markDeletePending(account, null);
  return true;
}

/**
 * Delete the account's left setup on the server and in this browser. When
 * the server misses the delete, this browser keeps a mark and the next load
 * sends it again.
 *
 * Args:
 *   account: The signed-in account.
 *   server: The server's draft calls.
 */
export function deleteSetupDraft(account: string, server: SetupDraftServer): Promise<void> {
  deletes += 1;
  const held = takenBody;
  const tab = readSessionValue(WIZARD_SESSION_KEY);
  take(null);
  clearLocalDraftOf(account);
  markDeletePending(account, deleteRevision(account) ?? 0);
  return queued(async () => {
    // Saves queued before this delete may have raised the revision.
    const revision = deleteRevision(account);
    markDeletePending(account, revision ?? 0);
    await sendDelete(
      account,
      server,
      revision ?? 0,
      SETUP_DRAFT_WRITE_TIMEOUT_MS,
      revision === null,
      held,
      tab,
    );
  });
}

/**
 * Read the account's left setup. A copy in this browser goes to the server
 * once and then leaves this browser. It wins over the server's draft when it
 * holds an edit the server never took; it is dropped when its save landed
 * already, or when a submit or Start new came after it. When the server
 * cannot answer in time, the copy in this browser is the draft.
 * While a missed delete cannot reach the server, only this browser's copy is offered.
 *
 * Args:
 *   account: The signed-in account.
 *   server: The server's draft calls.
 *
 * Returns:
 *   The draft, or null when there is none.
 */
export function loadSetupDraft(
  account: string,
  server: SetupDraftServer,
): Promise<SetupDraft | null> {
  return queued(async () => {
    // A reload reopens the tab's own setup, not the server's draft.
    const tabSetup = readSessionValue(WIZARD_SESSION_KEY);
    const tabTaken = tabSetup !== null ? readSessionValue(TAKEN_BODY_KEY) : null;
    const reopened = tabTaken?.startsWith(`${account}\n`) ? tabTaken : null;
    take(null);
    foreign = null;
    const settle = (server: SetupDraft | null, tabBody: string | null) => {
      const serverBody = server ? bodyKey(account, toServerDraft(server)) : null;
      if (tabBody === null || tabBody === serverBody) return take(serverBody);
      take(tabBody);
      // Same answers with another chat (a copy without it, a trimmed one, or a
      // chat-only edit elsewhere) is still this setup, so a submit deletes it.
      if (server?.values[WIZARD_SESSION_KEY] === tabSetup) return;
      foreign = account;
      // The server holds another setup, so this tab's saves must not land on it.
      known = null;
    };
    const local = localDraftOf(account);
    // The server's draft is unknown, so a delete from the reopened tab only
    // removes a draft that is still the tab's own setup.
    const unreachable = () => {
      if (reopened !== null) {
        take(reopened);
        foreign = account;
      }
      return local;
    };
    const deletedAt = deletePending(account);
    // The delete cleared this browser's copy, so a copy here is a newer setup;
    // the server's draft may still be the deleted one, so it is not offered.
    if (
      deletedAt !== null &&
      !(await sendDelete(account, server, deletedAt, SETUP_DRAFT_LOAD_TIMEOUT_MS))
    ) {
      return unreachable();
    }
    let slot: ServerSetupDraftSlot;
    try {
      slot = learn(
        account,
        await withTimeout((signal) => server.get(signal), SETUP_DRAFT_LOAD_TIMEOUT_MS),
      );
    } catch {
      return unreachable();
    }
    const remote = slot.draft ? fromServerDraft(account, slot.draft) : null;
    if (!local) {
      settle(remote, reopened);
      return remote;
    }
    const offered = { account, leftAt: local.leftAt, values: local.values };
    // The server refuses the copy when its save landed already, or when a
    // submit or Start new came after the revision it builds on. Otherwise the
    // copy holds an edit the server never took, and it goes on top even when
    // another device saved since.
    const mark = { ...local.save, copyBase: local.baseRevision ?? 0 };
    const sendCopy = async (base: number) =>
      learn(
        account,
        await withTimeout(
          (signal) => server.put(toServerDraft(local), base, false, signal, mark),
          SETUP_DRAFT_LOAD_TIMEOUT_MS,
        ),
      );
    // The copy can come from another tab; only a copy of the setup this tab reopens is its own.
    const copyIsTab = tabSetup !== null && local.values[WIZARD_SESSION_KEY] === tabSetup;
    const tabBody = copyIsTab ? bodyKey(account, toServerDraft(local)) : reopened;
    // The copy goes on top only of the revision it builds on, of the same
    // setup, or of an empty slot (the server refuses it after a delete); a
    // newer setup from another device stays.
    // The setup this tab reopens counts only for the tab's own copy or a copy
    // with no base: another tab's copy refused as superseded must not replace it.
    const tabCopy = copyIsTab || local.baseRevision === undefined;
    const ownSlot = (at: ServerSetupDraftSlot) => {
      if (!at.draft || at.revision === mark.copyBase) return true;
      // An earlier save of the page that made the copy landed after the copy's base;
      // the server refuses the copy when the copy's own save landed.
      if (at.writer != null && at.writer === mark.writer) return true;
      const left = fromServerDraft(account, at.draft);
      return (
        left.values[WIZARD_SESSION_KEY] === local.values[WIZARD_SESSION_KEY] ||
        (tabCopy &&
          (bodyKey(account, toServerDraft(left)) === reopened ||
            (tabSetup !== null && left.values[WIZARD_SESSION_KEY] === tabSetup)))
      );
    };
    const superseded = (at: ServerSetupDraftSlot) => {
      clearSetupDraft();
      const current = at.draft ? fromServerDraft(account, at.draft) : null;
      settle(current, tabBody);
      return current;
    };
    if (!ownSlot(slot)) return superseded(slot);
    let base = slot.revision;
    let saved: ServerSetupDraftSlot;
    try {
      saved = await sendCopy(base);
      // Another write came between the read and this save, so the copy goes once more on top.
      if (!saved.changed && saved.revision !== base) {
        if (!ownSlot(saved)) return superseded(saved);
        base = saved.revision;
        saved = await sendCopy(base);
      }
    } catch {
      return offered;
    }
    if (saved.changed) {
      clearSetupDraft();
      settle(local, tabBody);
      return offered;
    }
    if (saved.revision !== base) {
      // Writes keep coming; the next load decides. Until then a delete from this page misses.
      foreign = account;
      return offered;
    }
    clearSetupDraft();
    const current = saved.draft ? fromServerDraft(account, saved.draft) : null;
    settle(current, tabBody);
    return current;
  });
}
