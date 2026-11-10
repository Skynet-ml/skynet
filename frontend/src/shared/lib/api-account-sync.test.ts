import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = key => key; export const formatMsg = key => key;",
  "@/shared/lib/oauth-return": "export const forgetOAuthReturn = () => {};",
  "@/shared/lib/i18n": "export const I18N_KEY = {}; export const tI18n = key => key;",
  "@/shared/lib/report-error": "export const reportHandledError = () => {};",
  "@/shared/lib/account-missing": "export const ACCOUNT_MISSING_CODE = 'missing'; export const announceAccountMissing = () => {}; export const isAccountMissingResponse = () => false;",
  "@/shared/lib/runtime-env": "export const getRuntimeEnv = () => ({ apiUrl: 'https://desktop.test' }); export const isLocalMode = () => globalThis.accountTestLocal;",
  "@/shared/hooks/use-linked-account": "export const wasLinked = () => false;",
  "@/shared/lib/runtime-locale": "export const getActiveLocale = () => 'en';",
  "@/shared/lib/sse": "export const readServerSentEvents = () => {};",
};

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/lib/api-route") {
      return { shortCircuit: true, url: new URL("./api-route.ts", import.meta.url).href };
    }
    if (specifier === "@/shared/lib/resource-mode") {
      return { shortCircuit: true, url: new URL("./resource-mode.ts", import.meta.url).href };
    }
    if (stubs[specifier]) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}` };
    }
    return nextResolve(specifier, context);
  },
});

const api = await import("./api.ts");

test("account reads and writes use hosted on Mac even before link cache loads", async () => {
  const calls: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return Response.json({ items: [{ id: "alert", link: "/optimizations/run?pair=1#results" }], next_cursor: null });
  };
  try {
    for (const local of [true, false]) {
      Object.assign(globalThis, { accountTestLocal: local });
      calls.length = 0;
      await api.getNotificationPreferences();
      await api.updateNotificationPreferences({ hold_minutes: 10 });
      const page = await api.listNotifications("everything");
      await api.getNotificationUnreadCount();
      await api.markNotificationRead("alert");
      await api.markAllNotificationsRead("everything");
      await api.removeNotification("alert");
      await api.clearNotifications("everything");
      await api.getPushVapidKey();
      await api.savePushSubscription({ endpoint: "https://push.example.test/synthetic", keys: { p256dh: "test", auth: "test" } });
      await api.deletePushSubscription("https://push.example.test/synthetic");
      await api.getSlackConnection();
      await api.listSlackChannels();
      await api.updateSlackTarget(null);
      await api.disconnectSlack();
      await api.getBillingProfile();
      await api.getBillingTransactions();
      await api.getUsage();
      const prefix = `https://desktop.test${local ? "/local/hosted" : ""}/`;
      assert.equal(calls.length, 18);
      assert.ok(calls.every((url) => url.startsWith(prefix)), calls.join("\n"));
      assert.equal(page.items[0]?.link, local ? "/optimizations/run?pair=1&source=hosted#results" : "/optimizations/run?pair=1#results");
      await api.getUsage(undefined, undefined, "local");
      assert.equal(calls.at(-1), "https://desktop.test/billing/usage");
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("a missing hosted link fails without a local account fallback", async () => {
  const calls: string[] = [];
  const originalFetch = globalThis.fetch;
  Object.assign(globalThis, { accountTestLocal: true });
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return Response.json({ detail: "Link required" }, { status: 409 });
  };
  try {
    await assert.rejects(api.getBillingProfile(), { status: 409 });
    await assert.rejects(api.updateNotificationPreferences({ hold_minutes: 10 }), { status: 409 });
    assert.deepEqual(calls, [
      "https://desktop.test/local/hosted/billing/profile",
      "https://desktop.test/local/hosted/account/notification-preferences",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
