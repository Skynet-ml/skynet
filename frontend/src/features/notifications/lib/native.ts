/*
 * Native macOS notifications in the Mac app. The app's window is an Electron
 * renderer, where the web Notification API shows a notification in the macOS
 * Notification Center. In a plain browser on the same Mac the browser shows
 * it the same way. No service worker or push service is involved.
 */

import type { NativeNotification } from "@/shared/lib/api";

import type { PushPermission } from "./push-state";

/** Whether this window can show notifications at all. */
function nativeSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

/** The window's notification permission, or "unsupported". */
export function nativePermission(): PushPermission {
  return nativeSupported() ? Notification.permission : "unsupported";
}

/** Ask for permission (needs a click); resolves to the permission after the answer. */
export async function requestNativePermission(): Promise<PushPermission> {
  if (!nativeSupported()) return "unsupported";
  if (Notification.permission !== "default") return Notification.permission;
  return Notification.requestPermission();
}

/**
 * Show one claimed notification; a click brings the app forward on its page.
 * Returns whether it was shown, so only shown ones are confirmed.
 */
export function showNative(item: NativeNotification, open: (url: string) => void): boolean {
  if (nativePermission() !== "granted") return false;
  const notification = new Notification(item.title, { body: item.body, tag: item.tag });
  notification.onclick = () => {
    window.focus();
    open(item.url);
    notification.close();
  };
  return true;
}
