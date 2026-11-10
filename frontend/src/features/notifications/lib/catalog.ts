/*
 * The notification events and channels as the client sees them, mirroring
 * the backend's `core/notifications/catalog.py`. Pure (type imports only) so
 * the item wording and the grid rules can be unit tested without a browser.
 */

import type {
  LocalNotificationChannels,
  NotificationChannel,
  NotificationEventType,
  NotificationHeldChannel,
  NotificationItem,
} from "@/shared/lib/api";
import type { MessageKey } from "@/shared/lib/messages";

/** Grid row order: what needs the user first, product news last. */
export const EVENT_TYPES: readonly NotificationEventType[] = [
  "needs_input",
  "budget_near",
  "failed",
  "finished",
  "new_best",
  "stuck",
  "shared",
  "product_news",
];

export const CHANNELS: readonly NotificationChannel[] = ["inbox", "email", "chrome", "slack"];

/** A grid column: the hosted channels, plus macOS in the Mac app. */
export type GridChannel = NotificationChannel | "macos";

/** The Mac app's columns: every hosted channel stays, macOS is added. */
export const LOCAL_CHANNELS: readonly GridChannel[] = [...CHANNELS, "macos"];

export const HELD_CHANNELS: readonly NotificationHeldChannel[] = ["email", "chrome", "slack"];

/** The channels the hold can apply to: every delivery channel, so macOS too in the Mac app. */
export function heldChannels(local: LocalNotificationChannels | null): readonly GridChannel[] {
  return local ? [...HELD_CHANNELS, "macos"] : HELD_CHANNELS;
}

/** Whether a channel waits the hold. The macOS hold lives on the Mac app's own route. */
export function channelHeld(
  channel: GridChannel,
  held: readonly NotificationHeldChannel[],
  local: LocalNotificationChannels | null,
): boolean {
  if (channel === "macos") return local?.macos_held ?? false;
  return channel !== "inbox" && held.includes(channel);
}

/*
 * Channels every server delivers on. Slack is live only where the server has
 * a Slack app set up; elsewhere its column stays visible and disabled.
 */
export const LIVE_CHANNELS: ReadonlySet<GridChannel> = new Set(["inbox", "email", "chrome"]);

/** Whether a channel's column is usable on this server. */
export function channelLive(channel: GridChannel, slackConfigured: boolean): boolean {
  return channel === "slack" ? slackConfigured : LIVE_CHANNELS.has(channel);
}

/**
 * Only native macOS delivery depends on local configuration. Account channels
 * use hosted settings, even when the Mac has no local provider credentials.
 */
export function localOffReason(
  channel: GridChannel,
  local: LocalNotificationChannels | null,
): MessageKey | null {
  if (!local || channel !== "macos") return null;
  return local.channels[channel] === false ? "notifications.local.not_configured" : null;
}

/**
 * Preserve hosted preferences. Only unavailable native delivery shows off.
 */
export function cellShownOn(
  stored: boolean,
  channel: GridChannel,
  local: LocalNotificationChannels | null,
): boolean {
  return stored && localOffReason(channel, local) === null;
}

/**
 * Use hosted Slack availability and local native delivery availability.
 */
export function channelHeaderTip(
  channel: GridChannel,
  slackReady: boolean,
  local: LocalNotificationChannels | null,
): MessageKey[] {
  if (channel !== "macos") {
    return channelLive(channel, slackReady)
      ? [CHANNEL_KEYS[channel]]
      : ["notifications.slack.unavailable"];
  }
  const off = localOffReason(channel, local);
  return off ? [CHANNEL_KEYS[channel], off] : [CHANNEL_KEYS[channel]];
}

/** In the Mac app, the columns that are not set up on this Mac, in column order. */
export function localOffChannels(local: LocalNotificationChannels | null): GridChannel[] {
  return LOCAL_CHANNELS.filter((channel) => localOffReason(channel, local) !== null);
}

/**
 * Whether the page may be the Mac app. Local mode binds only to loopback, so
 * a hosted page never probes the local-only routes.
 */
export function maybeLocalHost(hostname: string): boolean {
  return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]";
}

/** Urgent events skip the hold and go out at once on every enabled channel. */
export const URGENT_EVENTS: ReadonlySet<NotificationEventType> = new Set([
  "needs_input",
  "budget_near",
]);

export const FOCUSED_EVENTS: ReadonlySet<NotificationEventType> = new Set([
  "needs_input",
  "budget_near",
  "failed",
  "finished",
]);

/** Whether a grid cell exists at all: product news only ever lands in the inbox. */
export function cellExists(event: NotificationEventType, channel: GridChannel): boolean {
  return event !== "product_news" || channel === "inbox";
}

/** Whether the user may flip a grid cell now. */
export function cellEditable(
  event: NotificationEventType,
  channel: GridChannel,
  slackConfigured = false,
  local: LocalNotificationChannels | null = null,
): boolean {
  // News is always in the inbox: the backend adds every entry for every user.
  if (event === "product_news") return false;
  if (channel === "macos") return local !== null && localOffReason(channel, local) === null;
  return channelLive(channel, slackConfigured);
}

export const EVENT_KEYS: Record<NotificationEventType, { label: MessageKey; tip: MessageKey }> = {
  needs_input: {
    label: "notifications.event.needs_input",
    tip: "notifications.event.needs_input_tip",
  },
  budget_near: {
    label: "notifications.event.budget_near",
    tip: "notifications.event.budget_near_tip",
  },
  failed: { label: "notifications.event.failed", tip: "notifications.event.failed_tip" },
  finished: { label: "notifications.event.finished", tip: "notifications.event.finished_tip" },
  new_best: { label: "notifications.event.new_best", tip: "notifications.event.new_best_tip" },
  stuck: { label: "notifications.event.stuck", tip: "notifications.event.stuck_tip" },
  shared: { label: "notifications.event.shared", tip: "notifications.event.shared_tip" },
  product_news: {
    label: "notifications.event.product_news",
    tip: "notifications.event.product_news_tip",
  },
};

export const CHANNEL_KEYS: Record<GridChannel, MessageKey> = {
  inbox: "notifications.channel.inbox",
  email: "notifications.channel.email",
  chrome: "notifications.channel.chrome",
  slack: "notifications.channel.slack",
  macos: "notifications.channel.macos",
};

/** How the item's numbers are rendered; injected so this module stays pure. */
export interface FactFormatters {
  score: (n: number) => string;
  /** A signed score change. */
  delta: (n: number) => string;
  usd: (dollars: number) => string;
  /** Seconds to a compact duration. */
  duration: (seconds: number) => string;
}

/** A catalog key with its numbers, or (for news) the headline as written. */
export type ItemLine = { key: MessageKey; params: Record<string, string> } | { text: string };

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * The item's one-line title: the event with its key number, in the
 * wording of the catalog key it picks. A fact the event did not record
 * falls back to the plain wording rather than a dash.
 */
export function itemLine(item: NotificationItem, fmt: FactFormatters): ItemLine {
  const { facts } = item;
  const score = num(facts.score);
  const delta = num(facts.delta);
  const spent = num(facts.spent);
  const limit = num(facts.limit);
  const duration = num(facts.duration);
  // A hardening job reuses the run events (SKY-360): it pauses for the user's
  // review as needs_input and ends as finished, with `kind: "hardening"`.
  // The facts type does not list that kind yet, so compare it as a string.
  if ((facts.kind as string | null | undefined) === "hardening") {
    if (item.event_type === "needs_input") return { key: "notifications.item.hardening_review", params: {} };
    if (item.event_type === "finished") return { key: "notifications.item.hardening_done", params: {} };
  }
  switch (item.event_type) {
    case "finished":
    case "new_best": {
      const base = item.event_type === "finished" ? "finished" : "new_best";
      if (score === null) {
        return { key: `notifications.item.${base}_plain` as MessageKey, params: {} };
      }
      if (delta === null) {
        return { key: `notifications.item.${base}` as MessageKey, params: { score: fmt.score(score) } };
      }
      return {
        key: `notifications.item.${base}_delta` as MessageKey,
        params: { score: fmt.score(score), delta: fmt.delta(delta) },
      };
    }
    case "failed":
      return duration === null
        ? { key: "notifications.item.failed_plain", params: {} }
        : { key: "notifications.item.failed", params: { duration: fmt.duration(duration) } };
    case "stuck":
      return score === null
        ? { key: "notifications.item.stuck_plain", params: {} }
        : { key: "notifications.item.stuck", params: { score: fmt.score(score) } };
    case "budget_near":
    case "needs_input": {
      const base = item.event_type;
      if (spent === null || limit === null) {
        return { key: `notifications.item.${base}_plain` as MessageKey, params: {} };
      }
      return {
        key: `notifications.item.${base}` as MessageKey,
        params: { spent: fmt.usd(spent), limit: fmt.usd(limit) },
      };
    }
    case "shared": {
      const actor = typeof facts.actor === "string" && facts.actor ? facts.actor : null;
      if (!actor) return { key: "notifications.item.shared_plain", params: {} };
      const which = facts.kind === "role" || facts.kind === "transfer" ? facts.kind : "invite";
      return { key: `notifications.item.shared_${which}` as MessageKey, params: { actor } };
    }
    case "product_news":
      return { text: item.title };
  }
}

/** Append an older page, dropping anything already shown (a poll may have shifted the list). */
export function appendPage(
  current: readonly NotificationItem[],
  older: readonly NotificationItem[],
): NotificationItem[] {
  const seen = new Set(current.map((item) => item.id));
  return [...current, ...older.filter((item) => !seen.has(item.id))];
}

/**
 * Fold a fresh first page into the list: the new head replaces the old one
 * and any older items already loaded by scrolling stay below it.
 */
export function mergeHead(
  current: readonly NotificationItem[],
  head: readonly NotificationItem[],
): NotificationItem[] {
  if (head.length === 0) return [];
  const oldest = head[head.length - 1]!.created_at;
  const headIds = new Set(head.map((item) => item.id));
  const tail = current.filter((item) => !headIds.has(item.id) && item.created_at < oldest);
  return [...head, ...tail];
}
