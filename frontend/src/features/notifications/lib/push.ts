/*
 * Chrome web push in the browser: register `public/sw.js`, subscribe with
 * the server's VAPID key, and hand the subscription to the backend (which
 * then delivers the Chrome channel to it). Every entry point is safe to call
 * on browsers without web push; they report "unsupported" and do nothing.
 */

import { getPushVapidKey, savePushSubscription } from "@/shared/lib/api";

import {
  shouldSyncPush,
  vapidKeyBytes,
  type PushPermission,
  type PushSubscribeResult,
} from "./push-state";

const WORKER_URL = "/sw.js";

/** Whether this browser can do web push at all (secure context, SW, PushManager). */
function pushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    window.isSecureContext &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

/** The browser's notification permission, or "unsupported". */
export function pushPermission(): PushPermission {
  return pushSupported() ? Notification.permission : "unsupported";
}

async function registration(): Promise<ServiceWorkerRegistration> {
  await navigator.serviceWorker.register(WORKER_URL, { scope: "/" });
  return navigator.serviceWorker.ready;
}

/** This browser's current push subscription, or null. */
export async function currentPushSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  const existing = await navigator.serviceWorker.getRegistration("/");
  return existing ? existing.pushManager.getSubscription() : null;
}

function serialize(subscription: PushSubscription) {
  const json = subscription.toJSON();
  return {
    endpoint: subscription.endpoint,
    keys: { p256dh: json.keys?.p256dh ?? "", auth: json.keys?.auth ?? "" },
  };
}

/**
 * Ask for permission when needed, subscribe this browser, and save it on the
 * server. Must start from a user gesture: Chrome ignores a permission request
 * that does not.
 */
export async function subscribePush(): Promise<PushSubscribeResult> {
  if (!pushSupported()) return "unsupported";
  try {
    let permission = Notification.permission;
    if (permission === "default") permission = await Notification.requestPermission();
    if (permission === "denied") return "denied";
    if (permission !== "granted") return "dismissed";

    const { public_key: key } = await getPushVapidKey();
    if (!key) return "unavailable";
    const reg = await registration();
    const applicationServerKey = vapidKeyBytes(key);
    let subscription: PushSubscription;
    try {
      subscription = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });
    } catch (err) {
      // A subscription made under an older VAPID key blocks a new one; drop it and retry.
      if (!(err instanceof DOMException) || err.name !== "InvalidStateError") throw err;
      await (await reg.pushManager.getSubscription())?.unsubscribe();
      subscription = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey });
    }
    await savePushSubscription(serialize(subscription));
    return "subscribed";
  } catch {
    return "error";
  }
}

/**
 * Re-save a subscription this browser already holds (it may belong to another
 * account here). Never asks for permission, so it is safe to call on any run start.
 */
export async function syncPushSubscription(): Promise<boolean> {
  if (!shouldSyncPush(pushPermission())) return false;
  try {
    const subscription = await currentPushSubscription();
    if (!subscription) return false;
    await savePushSubscription(serialize(subscription));
    return true;
  } catch {
    return false;
  }
}
