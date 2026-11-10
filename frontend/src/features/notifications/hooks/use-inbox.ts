"use client";

import * as React from "react";
import { toast } from "react-toastify";

import {
  clearNotifications,
  getNotificationUnreadCount,
  listNotifications,
  markAllNotificationsRead,
  markNotificationRead,
  removeNotification,
  type NotificationItem,
  type NotificationTab,
  type NotificationUnreadCounts,
} from "@/shared/lib/api";
import { onLinkChange } from "@/shared/hooks/use-linked-account";
import { msg } from "@/shared/lib/messages";

import { appendPage, mergeHead } from "../lib/catalog";

export const INBOX_POLL_MS = 30_000;

/**
 * Run `tick` now and every `intervalMs` while `enabled`, pausing while the
 * tab is hidden and catching up the moment it shows again. A new
 * `restartKey` runs it again at once and restarts the interval.
 */
function usePolling(
  tick: () => void,
  intervalMs: number,
  enabled: boolean,
  restartKey?: string,
) {
  const onTick = React.useEffectEvent(tick);
  React.useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer !== null) return;
      onTick();
      timer = setInterval(() => onTick(), intervalMs);
    };
    const stop = () => {
      if (timer !== null) clearInterval(timer);
      timer = null;
    };
    const onVisibility = () => (document.hidden ? stop() : start());
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled, intervalMs, restartKey]);
}

const NO_UNREAD: NotificationUnreadCounts = { focused: 0, everything: 0 };

export type InboxStatus = "loading" | "ready" | "error";

export interface Inbox {
  counts: NotificationUnreadCounts;
  items: NotificationItem[];
  status: InboxStatus;
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => void;
  retry: () => void;
  markRead: (item: NotificationItem) => void;
  markAllRead: () => void;
  remove: (item: NotificationItem) => void;
  clearAll: () => void;
}

/**
 * The inbox behind the bell: the unread counts and the selected tab's list,
 * both polled while the user is signed in so the panel opens on a list that
 * is already there, and refreshed again the moment it opens. A
 * read is applied on screen at once and sent in the background; a removal
 * too, rolled back with a toast if the server refuses it.
 */
export function useInbox({
  enabled,
  open,
  tab,
}: {
  enabled: boolean;
  open: boolean;
  tab: NotificationTab;
}): Inbox {
  const [accountRevision, setAccountRevision] = React.useState(0);
  const identity = `${tab}:${accountRevision}`;
  React.useEffect(() => onLinkChange(() => setAccountRevision((n) => n + 1)), []);
  const [counts, setCounts] = React.useState<NotificationUnreadCounts>(NO_UNREAD);
  const [items, setItems] = React.useState<NotificationItem[]>([]);
  const [status, setStatus] = React.useState<InboxStatus>("loading");
  const [cursor, setCursor] = React.useState<string | null>(null);
  const [loadingMore, setLoadingMore] = React.useState(false);
  // A response for a tab the user already left must not land on the new one.
  const tabRef = React.useRef(identity);
  React.useLayoutEffect(() => {
    tabRef.current = identity;
  }, [identity]);
  // Older pages are only fetched once the head has set where they start.
  const headLoaded = React.useRef(false);
  // Ids removed on screen but maybe not on the server yet: a poll racing the
  // DELETE must not bring them back.
  const removing = React.useRef(new Set<string>());
  const withoutRemoving = React.useCallback(
    (list: NotificationItem[]) =>
      removing.current.size ? list.filter((item) => !removing.current.has(item.id)) : list,
    [],
  );

  const refreshCounts = React.useCallback(() => {
    const requested = identity;
    getNotificationUnreadCount()
      .then((next) => { if (tabRef.current === requested) setCounts(next); })
      .catch(() => undefined);
  }, [identity]);

  const refreshHead = React.useCallback(() => {
    const requested = identity;
    listNotifications(tab)
      .then((page) => {
        if (tabRef.current !== requested) return;
        const head = withoutRemoving(page.items);
        setItems((prev) => (headLoaded.current ? mergeHead(prev, head) : head));
        // Once the user scrolled past the first page, keep the deeper cursor.
        if (!headLoaded.current) setCursor(page.next_cursor);
        headLoaded.current = true;
        setStatus("ready");
      })
      .catch(() => {
        if (tabRef.current !== requested) return;
        // A failed background poll keeps what is on screen.
        if (!headLoaded.current) setStatus("error");
      });
  }, [tab, identity, withoutRemoving]);

  // Switching tabs starts that list over; the poll below fetches it at once.
  React.useEffect(() => {
    headLoaded.current = false;
    removing.current.clear();
    setCounts(NO_UNREAD);
    setLoadingMore(false);
    setItems([]);
    setCursor(null);
    setStatus("loading");
  }, [identity]);

  const refreshAll = () => {
    refreshHead();
    refreshCounts();
  };
  usePolling(refreshAll, INBOX_POLL_MS, enabled, identity);

  // Opening the panel shows what was prefetched and checks for anything newer.
  const onOpen = React.useEffectEvent(refreshAll);
  React.useEffect(() => {
    if (enabled && open) onOpen();
  }, [enabled, open]);

  const loadMore = React.useCallback(() => {
    if (!cursor || loadingMore) return;
    const requested = identity;
    setLoadingMore(true);
    listNotifications(tab, cursor)
      .then((page) => {
        if (tabRef.current !== requested) return;
        setItems((prev) => appendPage(prev, withoutRemoving(page.items)));
        setCursor(page.next_cursor);
      })
      .catch(() => undefined)
      .finally(() => { if (tabRef.current === requested) setLoadingMore(false); });
  }, [cursor, loadingMore, tab, identity, withoutRemoving]);

  const retry = React.useCallback(() => {
    setStatus("loading");
    refreshHead();
  }, [refreshHead]);

  const markRead = React.useCallback((item: NotificationItem) => {
    if (item.read_at) return;
    const now = new Date().toISOString();
    setItems((prev) => prev.map((it) => (it.id === item.id ? { ...it, read_at: now } : it)));
    setCounts((prev) => ({
      focused: item.focused ? Math.max(0, prev.focused - 1) : prev.focused,
      everything: Math.max(0, prev.everything - 1),
    }));
    markNotificationRead(item.id).catch(() => undefined);
  }, []);

  const markAllRead = React.useCallback(() => {
    const now = new Date().toISOString();
    setItems((prev) => prev.map((it) => (it.read_at ? it : { ...it, read_at: now })));
    setCounts((prev) =>
      tab === "everything"
        ? NO_UNREAD
        : { focused: 0, everything: Math.max(0, prev.everything - prev.focused) },
    );
    markAllNotificationsRead(tab)
      .catch(() => undefined)
      .finally(refreshCounts);
  }, [tab, identity, refreshCounts]);

  const remove = React.useCallback(
    (item: NotificationItem) => {
      const ids = removing.current;
      if (ids.has(item.id)) return;
      ids.add(item.id);
      setItems((prev) => prev.filter((it) => it.id !== item.id));
      if (!item.read_at) {
        setCounts((prev) => ({
          focused: item.focused ? Math.max(0, prev.focused - 1) : prev.focused,
          everything: Math.max(0, prev.everything - 1),
        }));
      }
      removeNotification(item.id)
        .catch(() => {
          if (tabRef.current === identity) {
            setItems((prev) =>
              prev.some((it) => it.id === item.id)
                ? prev
                : [...prev, item].sort((a, b) => b.created_at.localeCompare(a.created_at)),
            );
          }
          toast.error(msg("notifications.remove_error"));
        })
        .finally(() => {
          ids.delete(item.id);
          refreshCounts();
        });
    },
    [tab, identity, refreshCounts],
  );

  const clearAll = React.useCallback(() => {
    const snapshot = items;
    const snapshotCursor = cursor;
    const ids = removing.current;
    snapshot.forEach((item) => ids.add(item.id));
    setItems([]);
    setCursor(null);
    setCounts((prev) =>
      tab === "everything"
        ? NO_UNREAD
        : { focused: 0, everything: Math.max(0, prev.everything - prev.focused) },
    );
    clearNotifications(tab)
      .catch(() => {
        if (tabRef.current === identity) {
          setItems((prev) => appendPage(prev, snapshot));
          setCursor(snapshotCursor);
        }
        toast.error(msg("notifications.clear_error"));
      })
      .finally(() => {
        snapshot.forEach((item) => ids.delete(item.id));
        refreshCounts();
      });
  }, [items, cursor, tab, identity, refreshCounts]);

  return {
    counts,
    items,
    status,
    hasMore: cursor !== null,
    loadingMore,
    loadMore,
    retry,
    markRead,
    markAllRead,
    remove,
    clearAll,
  };
}
