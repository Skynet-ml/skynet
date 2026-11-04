/**
 * One agent drives the whole setup in one conversation. Each screen it
 * reaches gets a turn of its own, and the agent's Continue / Back tools move
 * the wizard through the same code its buttons run.
 */

import type { AgentMessage } from "@/shared/ui/agent/types";

// Never shown, so not translated: the agent replies in the user's language
// either way, and one fixed text lets a reopened chat recognize it.
export const SCREEN_KICKOFF =
  "[The wizard opened this screen; I typed nothing.] If what you know is enough, fill this screen in and continue; otherwise ask me the one thing you need.";

/** A reopened chat stores the wizard's kickoff as a user turn; it stays unshown. */
export function hideKickoffs(messages: AgentMessage[]): AgentMessage[] {
  return messages.map((m) =>
    m.role === "user" && m.content.trim() === SCREEN_KICKOFF ? { ...m, hidden: true } : m,
  );
}

/** A move the screen agent asked for, read from its tool result. */
export type ScreenNav =
  | { action: "continue"; from: string }
  | { action: "back"; from: string; screen: string; request: string };

/** What the wizard frame exposes so the agent can move it. */
export interface ScreenNavigator {
  /** The screen on view. */
  screen: string;
  /** The Continue button's own handler. */
  forward: () => void;
  /** Jump back to an earlier screen. */
  goToScreen: (screen: string) => void;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Read a ``screen_nav`` move out of a tool result; null when there is none. */
export function parseScreenNav(result: unknown): ScreenNav | null {
  if (!isRecord(result) || !isRecord(result.screen_nav)) return null;
  const nav = result.screen_nav;
  const from = typeof nav.from === "string" ? nav.from : "";
  if (nav.action === "continue") return { action: "continue", from };
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
    navigator.forward();
  } else navigator.goToScreen(nav.screen);
}

/**
 * Where the setup's chat id is kept for the tab, with the screens its agent
 * already had a turn on, so a reload that reopens the setup reopens its chat
 * and does not start those screens over.
 */
export const SETUP_CHAT_KEY = "skynet.agent.setup-chat.v1";

/** The setup's chat as the tab keeps it. */
export interface SetupChat {
  id: string;
  kicked: string[];
}

/** Write the setup's chat for the tab. */
export function encodeSetupChat(chat: SetupChat): string {
  return JSON.stringify(chat);
}

/** Read the setup's chat back; null when nothing usable is kept. */
export function decodeSetupChat(raw: string | null): SetupChat | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!isRecord(value) || typeof value.id !== "string" || !value.id) return null;
    const kicked = Array.isArray(value.kicked)
      ? value.kicked.filter((s): s is string => typeof s === "string")
      : [];
    return { id: value.id, kicked };
  } catch {
    return null;
  }
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
