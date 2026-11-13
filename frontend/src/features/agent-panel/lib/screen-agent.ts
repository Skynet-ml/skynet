/**
 * One agent drives the whole setup in one conversation. Each screen it
 * reaches gets a turn of its own, and the agent's Continue / Back tools move
 * the wizard through the same code its buttons run.
 */

import { parseSourceKey, type ApiSource } from "@/shared/lib/api-route";
import { closeThinking } from "@/shared/lib/thinking-segments";
import type { AgentMessage } from "@/shared/ui/agent/types";

import { stopToolCalls } from "./pending-tool-calls";

// Never shown, so not translated: the agent replies in the user's language
// either way, and one fixed text lets a reopened chat recognize it.
export const SCREEN_KICKOFF =
  "[The wizard opened this screen; I typed nothing.] If what you know is enough, fill this screen in and continue; otherwise ask me the one thing you need.";

// Starts the hidden turn that carries the agent's Back request to the screen
// it reopens. The server stores no hidden flag, so this text marks the turn.
const SCREEN_BACK = "[The wizard went back to this screen for your request; I typed nothing.] ";

/** The hidden turn that hands the agent's Back request to the reopened screen. */
export function backRequest(request: string): string {
  return `${SCREEN_BACK}${request.trim()}`;
}

/**
 * Whether a screen opens with a kickoff turn. The Check screen does not: its
 * check starts as it opens, so a kickoff turn would have no result to explain
 * and could continue past the screen before the result lands. Only the
 * automatic failure message and the user's own messages start turns there.
 */
export function opensWithKickoff(screen: string): boolean {
  return screen !== "check";
}

/**
 * How a new setup chat starts on the screen the wizard opened. The screen a
 * restored setup reopens gets no kickoff, so it counts as started: a later
 * reload of the same screen must not send one either.
 *
 * Args:
 *   shown: The screen on view.
 *   reopened: The screen a restored setup reopened, or null.
 *
 * Returns:
 *   Whether the screen gets a kickoff, and the screens to count as started.
 */
export function newSetupChatStart(
  shown: string,
  reopened: string | null,
): { kickoff: boolean; kicked: string[] } {
  if (shown !== reopened && opensWithKickoff(shown)) return { kickoff: true, kicked: [] };
  return { kickoff: false, kicked: [shown] };
}

/** A reopened chat stores the wizard's kickoff and Back turns as user turns; they stay unshown. */
export function hideKickoffs(messages: AgentMessage[]): AgentMessage[] {
  return messages.map((m) =>
    m.role === "user" &&
    (m.content.trim() === SCREEN_KICKOFF || m.content.startsWith(SCREEN_BACK.trim()))
      ? { ...m, hidden: true }
      : m,
  );
}

/**
 * Copy the tab copy's hidden marks onto the server copy. The server stores a
 * hidden turn (such as the agent's Back request) as a plain user turn.
 *
 * Args:
 *   server: The server's messages.
 *   tab: The tab copy's messages.
 *
 * Returns:
 *   The server's messages, each user turn hidden when its matching tab turn was.
 */
export function keepHiddenTurns(
  server: AgentMessage[],
  tab: readonly AgentMessage[],
): AgentMessage[] {
  const tabTurns = tab.filter((m) => m.role === "user");
  let next = 0;
  return server.map((m) => {
    if (m.role !== "user") return m;
    // The user turns keep their order, so each one matches the next tab turn with its text.
    const at = tabTurns.findIndex((t, i) => i >= next && t.content === m.content);
    if (at < 0) return m;
    next = at + 1;
    return tabTurns[at]?.hidden && !m.hidden ? { ...m, hidden: true } : m;
  });
}

/**
 * Split off a chat's last user turn when no reply follows it.
 *
 * Args:
 *   messages: The chat's messages.
 *
 * Returns:
 *   The messages before that turn, and the turn (null when the last message is no user turn).
 */
export function unansweredTurn(messages: AgentMessage[]): {
  kept: AgentMessage[];
  turn: AgentMessage | null;
} {
  const last = messages[messages.length - 1];
  if (last?.role !== "user") return { kept: messages, turn: null };
  return { kept: messages.slice(0, -1), turn: last };
}

/** A move the screen agent asked for, read from its tool result. */
export type ScreenNav =
  /** ``to`` is a later screen to jump to, after a go-back edit: the user returns where they were. */
  | { action: "continue"; from: string; to?: string }
  | { action: "back"; from: string; screen: string; request: string };

/** What the wizard frame exposes so the agent can move it. */
export interface ScreenNavigator {
  /** The screen on view. */
  screen: string;
  /** The Continue button's handler, without its optimize.toml check: the server holds the agent's moves on that check. */
  forward: () => void;
  /** Jump back to an earlier screen. */
  goToScreen: (screen: string) => void;
  /** Jump forward to a later screen; the Continue button's checks, except the optimize.toml check, still apply. */
  goForwardTo: (screen: string) => void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Read a ``screen_nav`` move out of a tool result; null when there is none. */
export function parseScreenNav(result: unknown): ScreenNav | null {
  if (!isRecord(result) || !isRecord(result.screen_nav)) return null;
  const nav = result.screen_nav;
  const from = typeof nav.from === "string" ? nav.from : "";
  if (nav.action === "continue") {
    return typeof nav.to === "string" && nav.to
      ? { action: "continue", from, to: nav.to }
      : { action: "continue", from };
  }
  if (nav.action === "back" && typeof nav.screen === "string" && nav.screen) {
    const request = typeof nav.request === "string" ? nav.request : "";
    return { action: "back", from, screen: nav.screen, request };
  }
  return null;
}

/** Move the wizard as a screen agent asked, through the navigator's own handlers. */
export function runScreenNav(nav: ScreenNav, navigator: ScreenNavigator | null): void {
  if (!navigator) return;
  if (nav.action === "continue") {
    // The wizard may already have moved past the agent's screen by itself;
    // continuing again would skip the next one unseen.
    if (nav.from && nav.from !== navigator.screen) return;
    if (nav.to) navigator.goForwardTo(nav.to);
    else navigator.forward();
  } else navigator.goToScreen(nav.screen);
}

/**
 * The wizard's screens in order: the backend's ``_SCREEN_ORDER``, plus ``schedule``.
 */
const SCREEN_ORDER: readonly string[] = [
  "brief",
  "artifact",
  "source",
  "plan",
  "optimizer",
  "models",
  "budget",
  "metric",
  "check",
  "schedule",
  "overview",
];

/** The later of two screens in the wizard's order; null when both are null. */
function laterScreen(a: string | null, b: string | null): string | null {
  if (a === null || b === null) return a ?? b;
  return SCREEN_ORDER.indexOf(a) > SCREEN_ORDER.indexOf(b) ? a : b;
}

/**
 * The screen the user returns to after an agent's go-back edit. The first
 * go-back sets it; a second go-back from a screen before it keeps it. A
 * go-back from a later screen replaces it, since the user moved past it. Any
 * other move keeps it until the wizard reaches it, even a Continue whose
 * jump the wizard holds.
 *
 * Args:
 *   current: The return screen so far, or null.
 *   nav: The move the agent asked for.
 *   shown: The screen the wizard shows when the move arrives. The user can
 *     move on while the agent works, so it can be later than ``nav.from``.
 *
 * Returns:
 *   The return screen after the move.
 */
export function nextReturnScreen(
  current: string | null,
  nav: ScreenNav,
  shown: string | null = null,
): string | null {
  if (nav.action !== "back") return current;
  return laterScreen(current, laterScreen(nav.from || null, shown));
}

/**
 * Whether the wizard reached the return screen: it shows that screen or a
 * later one, for example after a stepper jump past it.
 *
 * Args:
 *   returnScreen: The screen the user returns to.
 *   screen: The screen the wizard shows, or null.
 *
 * Returns:
 *   True when the return screen is no longer ahead of the wizard.
 */
export function reachedReturnScreen(returnScreen: string, screen: string | null): boolean {
  if (screen === null) return false;
  if (screen === returnScreen) return true;
  const here = SCREEN_ORDER.indexOf(screen);
  const there = SCREEN_ORDER.indexOf(returnScreen);
  return here >= 0 && there >= 0 && here > there;
}

/**
 * Where the setup's chat is kept for the tab: its id, the screens its agent
 * already had a turn on, and its messages. The server saves the chat like
 * any other, and the draft carries this copy to a reload or another device:
 * its id finds the server copy, and its started screens are not started over.
 */
export const SETUP_CHAT_KEY = "skynet.agent.setup-chat.v1";

/** The setup's chat as the tab keeps it. */
export interface SetupChat {
  id: string;
  kicked: string[];
  messages: AgentMessage[];
  /**
   * What each of the chat's wizard writes found on the form, by tool call
   * id: the before-state of each turn's edits, so the "Edited N files" card
   * and its Undo survive a reload.
   */
  ledger?: Record<string, Record<string, unknown>>;
}

/**
 * Keep only the before-states of the tool calls a chat still holds.
 *
 * Args:
 *   messages: The chat's messages.
 *   writeBefore: What each wizard write found on the form, by tool call id.
 *
 * Returns:
 *   The entries of the chat's own tool calls.
 */
export function setupChatLedger(
  messages: readonly AgentMessage[],
  writeBefore: Readonly<Record<string, Record<string, unknown>>>,
): Record<string, Record<string, unknown>> {
  const ledger: Record<string, Record<string, unknown>> = {};
  for (const message of messages) {
    for (const call of message.toolCalls ?? []) {
      const before = writeBefore[call.id];
      if (before) ledger[call.id] = before;
    }
  }
  return ledger;
}

// Browser storage holds about 5 MB per origin. The left setup's copy goes to
// localStorage as an escaped string inside the draft, next to everything
// else the app keeps there.
export const SETUP_CHAT_MAX_CHARS = 300_000;

/** The browser's own id for a setup chat until its first turn saves it on the server. */
export function newSetupChatId(): string {
  return `setup-${crypto.randomUUID()}`;
}

/** Whether an `open_page` route opens the setup wizard (SKY-631). */
export function opensSetup(path: string): boolean {
  return path.replace(/\/+$/, "") === "/submit";
}

/**
 * The chat that becomes the setup chat when the wizard opens after the agent
 * opened it from a chat (SKY-631). The same conversation continues in the
 * setup, so the chat history keeps one entry.
 *
 * Args:
 *   handoff: The chat whose agent opened the setup, or null.
 *   live: The chat on view as the wizard opens.
 *
 * Returns:
 *   The setup chat's id and messages, or null when the setup starts a new
 *   chat: no handoff, or the user moved to another chat. A chat that the
 *   server did not save gets a browser id; its next turn's server id
 *   replaces it.
 */
export function handedOverSetupChat(
  handoff: { id: string | null } | null,
  live: { id: string | null; messages: AgentMessage[] },
): { id: string; messages: AgentMessage[] } | null {
  if (!handoff || handoff.id !== live.id || live.messages.length === 0) return null;
  return { id: live.id ?? newSetupChatId(), messages: live.messages };
}

/**
 * The id the setup chat keeps when a turn's server conversation arrives. A
 * chat with no server copy yet takes the server's id, so the tab's copy and
 * the run it starts point at the saved conversation.
 *
 * Args:
 *   current: The setup chat's id, or null when no setup is open.
 *   serverId: The conversation the server saved the turn in.
 *   isActive: The turn's chat is the one on view, which in a setup is the setup chat.
 *
 * Returns:
 *   The id to keep, or null when nothing changes.
 */
export function adoptedSetupChatId(
  current: string | null,
  serverId: string,
  isActive: boolean,
): string | null {
  return current !== null && !isSavedSetupChat(current) && isActive ? serverId : null;
}

/**
 * Whether a setup chat id names a server conversation. A ``setup-`` id is the
 * browser's own: no turn saved the chat yet, its server copy failed to load,
 * or an older build kept the chat only in the tab. Its next turn saves it.
 */
export function isSavedSetupChat(id: string): boolean {
  return !id.startsWith("setup-");
}

/**
 * Whether the server's copy of a reopened setup chat may replace the tab's
 * copy on view: the chat is still the setup's, and no turn changed it.
 *
 * Args:
 *   setupId: The setup chat's id now.
 *   live: The chat on view.
 *   opened: The setup chat's id and the messages it opened with.
 *
 * Returns:
 *   True when the loaded copy may take over.
 */
export function setupChatUnchanged(
  setupId: string | null,
  live: { id: string | null; messages: readonly AgentMessage[] },
  opened: { id: string; messages: readonly AgentMessage[] },
): boolean {
  return setupId === opened.id && live.id === opened.id && live.messages === opened.messages;
}

/**
 * Whether the setup chat on view lost its server copy: its last turn failed
 * because the server has no conversation under its id (deleted elsewhere).
 *
 * Args:
 *   setupId: The setup chat's id now.
 *   liveId: The id of the chat on view.
 *   errorCode: The machine code of the chat's last error.
 *
 * Returns:
 *   True when the setup chat must move to a new browser id.
 */
export function setupChatGone(
  setupId: string | null,
  liveId: string | null,
  errorCode: string | null,
): boolean {
  return setupId !== null && liveId === setupId && errorCode === "agent.conversation.not_found";
}

/**
 * Write the setup's chat for the tab, within ``SETUP_CHAT_MAX_CHARS``. A chat
 * too long first loses its tool results and its ledger, then its oldest messages.
 */
export function encodeSetupChat(chat: SetupChat): string {
  const full = JSON.stringify(chat);
  if (full.length <= SETUP_CHAT_MAX_CHARS) return full;
  // The edits read each write's result, so without results the ledger is dead weight.
  const { ledger: _ledger, ...lean } = chat;
  const messages = lean.messages.map((m) =>
    m.toolCalls?.length
      ? { ...m, toolCalls: m.toolCalls.map(({ payload: _, ...call }) => call) }
      : m,
  );
  // Each message costs its own text plus one comma. The array has one comma
  // fewer than messages, so starting one below the empty chat makes the sum exact.
  let size = JSON.stringify({ ...lean, messages: [] }).length - 1;
  let start = messages.length;
  while (start > 0) {
    const next = size + JSON.stringify(messages[start - 1]).length + 1;
    if (next > SETUP_CHAT_MAX_CHARS) break;
    size = next;
    start--;
  }
  return JSON.stringify({ ...lean, messages: messages.slice(start) });
}

/** Read the setup's chat back; null when nothing usable is kept. */
export function decodeSetupChat(raw: string | null): SetupChat | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value) || typeof value.id !== "string" || !value.id) return null;
    if (!Array.isArray(value.messages)) return null;
    const kicked = Array.isArray(value.kicked)
      ? value.kicked.filter((s): s is string => typeof s === "string")
      : [];
    const messages = value.messages.filter(
      (m): m is AgentMessage =>
        isRecord(m) &&
        (m.role === "user" || m.role === "assistant") &&
        typeof m.content === "string",
    );
    const ledger = isRecord(value.ledger)
      ? Object.fromEntries(Object.entries(value.ledger).filter(([, before]) => isRecord(before)))
      : {};
    return Object.keys(ledger).length
      ? {
          id: value.id,
          kicked,
          messages,
          ledger: ledger as Record<string, Record<string, unknown>>,
        }
      : { id: value.id, kicked, messages };
  } catch {
    return null;
  }
}

/**
 * The server id of a setup chat an older build kept: it has an id and no
 * messages, because the server saved the chat. Null for anything else.
 */
export function legacySetupChatId(raw: string | null): string | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value) || typeof value.id !== "string" || !value.id) return null;
    return Array.isArray(value.messages) ? null : value.id;
  } catch {
    return null;
  }
}

/**
 * The server id of the setup chat that a submitted run keeps. The run's
 * owner, admins and editors can read this chat; viewers cannot. The owner
 * reads it raw, and the others read it with secrets masked.
 *
 * Args:
 *   raw: The tab's stored setup chat.
 *   runSource: The server the run starts on.
 *
 * Returns:
 *   The chat's id on that server, or null when the chat has no server copy
 *   yet or lives on the other server.
 */
export function runSetupChatId(raw: string | null, runSource: ApiSource): string | null {
  const chat = decodeSetupChat(raw);
  if (!chat) return null;
  const { source, id } = parseSourceKey(chat.id);
  return source === runSource && isSavedSetupChat(id) ? id : null;
}

/**
 * The messages a setup chat keeps when the tab goes away mid-turn. A kickoff
 * still running is cut off, so the reopened screen runs it again; what the
 * user wrote during it stays. Any other turn keeps the user's message and
 * the reply so far. The last reply's open tool rows and thinking end, as a
 * stop ends them: a reopened chat gets no event that would end them, and a
 * running write would keep its fields locked.
 *
 * Args:
 *   messages: The chat as shown.
 *   kickoffFrom: Where a running kickoff turn starts, or null.
 *   now: The current time, in epoch ms.
 *
 * Returns:
 *   The messages to store.
 */
export function setupChatSnapshot(
  messages: AgentMessage[],
  kickoffFrom: number | null,
  now: number,
): AgentMessage[] {
  const kept =
    kickoffFrom === null
      ? messages.slice()
      : [
          ...messages.slice(0, kickoffFrom),
          ...messages.slice(kickoffFrom).filter((m) => m.role === "user" && !m.hidden),
        ];
  const last = kept[kept.length - 1];
  if (last?.role !== "assistant") return kept;
  const ended: AgentMessage = {
    ...last,
    ...(last.toolCalls ? { toolCalls: stopToolCalls(last.toolCalls, now) } : {}),
    ...(last.thinkingSegments
      ? { thinkingSegments: closeThinking(last.thinkingSegments, now) }
      : {}),
  };
  if (!ended.content && !ended.toolCalls?.length) kept.pop();
  else kept[kept.length - 1] = ended;
  return kept;
}

/** A kickoff turn the panel sent, until the agent finishes it. */
export interface KickoffTurn {
  screen: string;
  /** Index of the kickoff's own message in the chat. */
  from: number;
  /** The agent started on it; a send takes a render to show as busy. */
  started: boolean;
}

/**
 * Follow a kickoff turn through the agent's busy flag.
 *
 * Returns:
 *   The turn still in flight, or null once it ended.
 */
export function trackKickoffTurn(turn: KickoffTurn | null, busy: boolean): KickoffTurn | null {
  if (!turn) return null;
  if (busy) return turn.started ? turn : { ...turn, started: true };
  return turn.started ? null : turn;
}

/** The screens the tab's copy counts as started: a kickoff still running does not count. */
export function storedKickedScreens(
  kicked: Iterable<string>,
  running: KickoffTurn | null,
): string[] {
  return [...kicked].filter((s) => s !== running?.screen);
}

let setupChatFlush: (() => void) | null = null;

/**
 * Let the panel write the setup chat on demand. The wizard calls
 * ``flushSetupChat`` right before it copies the tab's setup keys, because
 * the order of two ``pagehide`` listeners depends on which mounted first.
 *
 * Returns:
 *   A function that removes the writer.
 */
export function registerSetupChatFlush(flush: () => void): () => void {
  setupChatFlush = flush;
  return () => {
    if (setupChatFlush === flush) setupChatFlush = null;
  };
}

/** Write the setup chat's current messages to the tab now. */
export function flushSetupChat(): void {
  setupChatFlush?.();
}

/**
 * What the panel does with a message the wizard queued for the agent.
 *
 * Args:
 *   input.docked: The chat is on view, so the user sees the exchange.
 *   input.busy: A turn is running; two at once leave tool calls hanging.
 *   input.silent: A cloned run whose user has not written to the agent yet.
 *
 * Returns:
 *   "skip" drops it, "wait" holds it, "send" sends it now.
 */
export function autoFeedbackAction(input: {
  docked: boolean;
  busy: boolean;
  silent: boolean;
}): "skip" | "wait" | "send" {
  if (input.silent) return "skip";
  if (!input.docked || input.busy) return "wait";
  return "send";
}

/** An automatic turn the panel sent, until the agent finishes it. */
export interface AutoTurn {
  id: number;
  /** The agent started on it; a send takes a render to show as busy. */
  started: boolean;
}

/**
 * Follow an automatic turn through the agent's busy flag.
 *
 * Args:
 *   turn: The turn in flight, or null.
 *   busy: The agent is running or waiting on a turn.
 *
 * Returns:
 *   The turn still in flight, and the id of the one that just ended.
 */
export function trackAutoTurn(
  turn: AutoTurn | null,
  busy: boolean,
): { turn: AutoTurn | null; ended: number | null } {
  if (!turn) return { turn: null, ended: null };
  if (busy) return { turn: turn.started ? turn : { ...turn, started: true }, ended: null };
  return turn.started ? { turn: null, ended: turn.id } : { turn, ended: null };
}
