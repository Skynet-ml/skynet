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
  "@/shared/hooks/use-linked-account": "export const wasLinked = () => globalThis.accountTestLinked === true;",
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
    return Response.json({});
  };
  try {
    for (const local of [true, false]) {
      Object.assign(globalThis, { accountTestLocal: local });
      calls.length = 0;
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
      assert.equal(calls.length, 10);
      assert.ok(calls.every((url) => url.startsWith(prefix)), calls.join("\n"));
      await api.getUsage(undefined, undefined, "local");
      assert.equal(calls.at(-1), "https://desktop.test/billing/usage");
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

/** Call every inbox and notification-settings function; returns the first page. */
async function callInbox() {
  await api.getNotificationPreferences();
  await api.updateNotificationPreferences({ hold_minutes: 10 });
  const page = await api.listNotifications("everything");
  await api.getNotificationUnreadCount();
  await api.markNotificationRead("alert");
  await api.markAllNotificationsRead("everything");
  await api.removeNotification("alert");
  await api.clearNotifications("everything");
  return page;
}

test("the inbox and its settings follow the Local/Web pick, local when not linked", async () => {
  const calls: string[] = [];
  const originalFetch = globalThis.fetch;
  const store = new Map<string, string>();
  const originalWindow = (globalThis as { window?: unknown }).window;
  Object.assign(globalThis, {
    window: {
      localStorage: { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => store.set(k, v) },
      dispatchEvent: () => true,
    },
  });
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return Response.json({ items: [{ id: "alert", link: "/optimizations/run?pair=1#results" }], next_cursor: null });
  };
  const cases = [
    { name: "web app", local: false, linked: false, pick: null, prefix: "https://desktop.test/", hosted: false },
    { name: "unlinked Mac", local: true, linked: false, pick: null, prefix: "https://desktop.test/", hosted: false },
    { name: "linked Mac, Local picked", local: true, linked: true, pick: "local", prefix: "https://desktop.test/", hosted: false },
    { name: "linked Mac, Web picked", local: true, linked: true, pick: "hosted", prefix: "https://desktop.test/local/hosted/", hosted: true },
  ];
  try {
    for (const c of cases) {
      Object.assign(globalThis, { accountTestLocal: c.local, accountTestLinked: c.linked });
      if (c.pick) store.set("skynet.resource-mode", c.pick);
      else store.clear();
      calls.length = 0;
      const page = await callInbox();
      assert.equal(calls.length, 8, c.name);
      assert.ok(calls.every((url) => url.startsWith(c.prefix) && (c.hosted || !url.includes("/local/hosted/"))), `${c.name}\n${calls.join("\n")}`);
      assert.equal(
        page.items[0]?.link,
        c.hosted ? "/optimizations/run?pair=1&source=hosted#results" : "/optimizations/run?pair=1#results",
        c.name,
      );
    }
  } finally {
    globalThis.fetch = originalFetch;
    Object.assign(globalThis, { window: originalWindow, accountTestLinked: false });
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
    await assert.rejects(api.getSlackConnection(), { status: 409 });
    assert.deepEqual(calls, [
      "https://desktop.test/local/hosted/billing/profile",
      "https://desktop.test/local/hosted/notifications/slack",
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
