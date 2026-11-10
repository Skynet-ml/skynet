import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import type { ScheduleRule } from "@/shared/types/continuous";

const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = key => key; export const formatMsg = key => key;",
  "@/shared/lib/oauth-return": "export const forgetOAuthReturn = () => {};",
  "@/shared/lib/i18n": "export const I18N_KEY = {}; export const tI18n = key => key;",
  "@/shared/lib/report-error": "export const reportHandledError = () => {};",
  "@/shared/lib/account-missing": "export const ACCOUNT_MISSING_CODE = 'missing'; export const announceAccountMissing = () => {}; export const isAccountMissingResponse = () => false;",
  "@/shared/lib/runtime-env": "export const getRuntimeEnv = () => ({ apiUrl: 'https://desktop.test' }); export const isLocalMode = () => true;",
  "@/shared/hooks/use-linked-account": "export const wasLinked = () => true;",
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

test("dashboard analytics uses the selected source and keeps date filters", async () => {
  const calls: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return Response.json({ filtered_total: 5 });
  };
  try {
    for (const source of ["local", "hosted"] as const) {
      const result = await api.getDashboardAnalytics({
        source,
        username: "desktop-user",
        date: "2026-07-01",
        date_to: "2026-08-31",
        include_shared: true,
      });
      assert.equal(result.filtered_total, 5);
      const url = new URL(calls.at(-1)!);
      assert.equal(url.pathname, `${source === "hosted" ? "/local/hosted" : ""}/analytics/dashboard`);
      assert.equal(url.searchParams.get("username"), source === "hosted" ? null : "desktop-user");
      assert.equal(url.searchParams.get("date"), "2026-07-01");
      assert.equal(url.searchParams.get("date_to"), "2026-08-31");
      assert.equal(url.searchParams.get("include_shared"), "true");
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("continuous and serving requests stay with their run's source", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return init?.method === "DELETE"
      ? new Response(null, { status: 204 })
      : Response.json({});
  };
  try {
    const schedule: ScheduleRule = {
      frequency: "daily",
      every: 1,
      time: "09:00",
      timezone: "UTC",
      weekdays: [],
      month_day: 1,
    };
    for (const source of ["local", "hosted"] as const) {
      calls.length = 0;
      await api.getContinuousState("run", source);
      await api.enableContinuous("run", { schedule, permitted: { task_model: true, reflection_model: false } }, source);
      await api.setContinuousPaused("plan", true, source);
      await api.setContinuousPaused("plan", false, source);
      await api.updateContinuousSchedule("plan", schedule, source);
      await api.reviewContinuousCycle("plan", "cycle", true, source);
      await api.reviewContinuousCycle("plan", "cycle", false, source);
      await api.deleteContinuous("plan", source);
      await api.getServeInfo("run", source);
      await api.setServingEnabled("run", true, source);
      await api.serveProgram("run", { question: "synthetic" }, "call-key", source);
      const prefix = `https://desktop.test${source === "hosted" ? "/local/hosted" : ""}`;
      assert.deepEqual(calls.map(({ url, init }) => [url.slice(prefix.length), init?.method ?? "GET"]), [
        ["/optimizations/run/continuous", "GET"],
        ["/optimizations/run/continuous", "POST"],
        ["/continuous/plan/pause", "POST"],
        ["/continuous/plan/resume", "POST"],
        ["/continuous/plan/schedule", "PUT"],
        ["/continuous/plan/cycles/cycle/approve", "POST"],
        ["/continuous/plan/cycles/cycle/reject", "POST"],
        ["/continuous/plan", "DELETE"],
        ["/serve/run/info", "GET"],
        ["/serve/run/enabled", "PUT"],
        ["/serve/run", "POST"],
      ]);
      assert.ok(calls.every(({ url }) => url.startsWith(prefix)));
      assert.equal(new Headers(calls.at(-1)?.init?.headers).get("Idempotency-Key"), "call-key");
      assert.deepEqual(JSON.parse(String(calls.at(-1)?.init?.body)), { inputs: { question: "synthetic" } });
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});
