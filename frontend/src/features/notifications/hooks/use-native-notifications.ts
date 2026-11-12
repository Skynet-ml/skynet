"use client";

import * as React from "react";
import { useRouter } from "next/navigation";

import {
  claimNativeNotifications,
  confirmNativeNotifications,
  getLocalNotificationChannels,
  type LocalNotificationChannels,
} from "@/shared/lib/api";

import { maybeLocalHost } from "../lib/catalog";
import { nativePermission, showNative } from "../lib/native";
import { probeUntilAnswered, showClaimed } from "../lib/native-poll";

const NATIVE_POLL_MS = 10_000;

/**
 * The Mac app's channel states, or null on a hosted server. Only loopback
 * pages ask, so hosted pages send no extra request. An error (the API is
 * restarting) is asked again every poll until the server answers.
 */
export function useLocalNotificationChannels(enabled: boolean) {
  const [local, setLocal] = React.useState<LocalNotificationChannels | null>(null);
  React.useEffect(() => {
    if (!enabled || !maybeLocalHost(window.location.hostname)) return;
    return probeUntilAnswered(getLocalNotificationChannels, setLocal, NATIVE_POLL_MS);
  }, [enabled]);
  return [local, setLocal] as const;
}

/**
 * In the Mac app, show queued macOS notifications while the app runs. The
 * timer also runs while the window is hidden, but Chromium slows timers in
 * hidden windows, so a notification can then come up to about a minute late.
 * A claim leases the notifications, so two windows never show one twice.
 * Only the ones shown are confirmed; a lost answer or a refused permission
 * lets the lease run out, and the server hands them out again. The server
 * expires what the app could not show within a day; the inbox keeps it.
 */
export function useNativeNotifications(enabled: boolean) {
  const router = useRouter();
  const [local] = useLocalNotificationChannels(enabled);
  const onTick = React.useEffectEvent(() => {
    if (nativePermission() !== "granted") return;
    claimNativeNotifications()
      .then(({ items }) => {
        const shown = showClaimed(items, (item) => showNative(item, (url) => router.push(url)));
        return shown.length > 0 ? confirmNativeNotifications(shown) : undefined;
      })
      .catch(() => undefined);
  });
  const active = enabled && local !== null;
  React.useEffect(() => {
    if (!active) return;
    onTick();
    const timer = setInterval(() => onTick(), NATIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [active]);
}
