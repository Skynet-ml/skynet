/**
 * One agent per wizard screen. Each screen keeps its own conversation, so
 * moving to a screen resumes the chat held there or starts a fresh one, and
 * the agent's Continue / Back tools move the wizard through the same code
 * its buttons run.
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

/** What the panel does with the chat when the screen on view changes. */
export type ScreenSwitch =
  | { kind: "stay" }
  | { kind: "resume"; conversationId: string }
  | { kind: "fresh" };

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
 * Decide which chat to show once the screen on view changes, and remember
 * the chat the user is leaving under its screen.
 *
 * @param chats Screen to conversation id; updated in place.
 * @param from The screen being left, or null when the wizard just mounted.
 * @param leavingConversationId The chat on view, null before its first turn.
 * @param to The screen now on view.
 */
export function switchScreenChat(
  chats: Map<string, string>,
  from: string | null,
  leavingConversationId: string | null,
  to: string,
): ScreenSwitch {
  if (from === to) return { kind: "stay" };
  if (from !== null && leavingConversationId) chats.set(from, leavingConversationId);
  const held = chats.get(to);
  return held ? { kind: "resume", conversationId: held } : { kind: "fresh" };
}

/**
 * Where each screen's chat id is kept for the tab, so a reload that reopens
 * the setup reopens each screen's chat with it.
 */
export const SCREEN_CHATS_KEY = "skynet.agent.screen-chats.v1";

/**
 * Read back the screen-to-chat map kept for the tab.
 *
 * @param raw The stored text, or null.
 * @returns The map; empty when missing or malformed.
 */
export function decodeScreenChats(raw: string | null): Map<string, string> {
  const chats = new Map<string, string>();
  if (!raw) return chats;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return chats;
  }
  if (!isRecord(parsed)) return chats;
  for (const [screen, id] of Object.entries(parsed)) {
    if (typeof id === "string" && id) chats.set(screen, id);
  }
  return chats;
}

/** Serialize the screen-to-chat map for the tab's session storage. */
export function encodeScreenChats(chats: ReadonlyMap<string, string>): string {
  return JSON.stringify(Object.fromEntries(chats));
}
