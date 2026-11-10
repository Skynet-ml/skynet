import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import type { DashboardAnalytics } from "@/shared/lib/api";

const stubs: Record<string, string> = {
  "@/shared/lib/api": "export const getDashboardAnalytics = params => globalThis.analyticsTestLoad(params);",
  "@/shared/hooks/use-linked-account": "export const onLinkChange = listener => globalThis.analyticsTestSubscribe(listener);",
};
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/hooks/use-source-swap") {
      return { shortCircuit: true, url: new URL("../../../shared/hooks/use-source-swap.ts", import.meta.url).href };
    }
    if (stubs[specifier]) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}` };
    }
    return nextResolve(specifier, context);
  },
});

const { useDashboardAnalytics } = await import("./use-dashboard-analytics.ts");
const { SWAP_MIN_MS } = await import("@/shared/hooks/use-source-swap");
const { createRoot } = await import("react-dom/client");

test("analytics refreshes keep charts mounted and source changes reject stale results", async () => {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://desktop.test" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  const pending: Array<{
    source: string;
    resolve: (data: DashboardAnalytics) => void;
    reject: (error: Error) => void;
  }> = [];
  let linkChanged: () => void = () => {};
  Object.assign(globalThis, {
    analyticsTestLoad: ({ source }: { source: string }) => new Promise<DashboardAnalytics>((resolve, reject) => {
      pending.push({ source, resolve, reject });
    }),
    analyticsTestSubscribe: (listener: () => void) => {
      linkChanged = listener;
      return () => { linkChanged = () => {}; };
    },
  });
  let current: ReturnType<typeof useDashboardAnalytics>;
  let mounts = 0;
  function Charts({ total }: { total: number }) {
    React.useEffect(() => { mounts += 1; }, []);
    return React.createElement("div", { "data-charts": true }, total);
  }
  function Probe({ source, range = "all" }: { source: "local" | "hosted"; range?: "all" | "30d" }) {
    current = useDashboardAnalytics({
      source, range, sessionUser: "desktop", isAdmin: false, activeTab: "analytics",
      optimizer: "all", model: "all", status: "all", date: null, dateTo: null,
      owner: null, access: null, improvement: null, runtime: null, dataset: null,
    });
    return current.analyticsData
      ? React.createElement(Charts, { total: current.analyticsData.filtered_total })
      : React.createElement("div", { "data-loading": true });
  }
  const root = createRoot(document.getElementById("root")!);
  const finish = async (request: number, total: number) => {
    await React.act(async () => pending[request]!.resolve({ filtered_total: total } as DashboardAnalytics));
  };
  try {
    await React.act(async () => root.render(React.createElement(Probe, { source: "hosted" })));
    await finish(0, 5);
    const charts = document.querySelector("[data-charts]");
    assert.ok(charts);
    assert.equal(mounts, 1);

    for (let request = 1; request <= 2; request += 1) {
      await React.act(async () => linkChanged());
      assert.equal(current!.analyticsLoading, true);
      assert.equal(document.querySelector("[data-charts]"), charts);
      await finish(request, 5);
      assert.equal(document.querySelector("[data-charts]"), charts);
      assert.equal(mounts, 1);
    }

    await React.act(async () => { void current!.fetchDashboardAnalytics(); });
    await React.act(async () => pending[3]!.reject(new Error("offline")));
    assert.equal(current!.analyticsError, true);
    assert.equal(current!.analyticsData?.filtered_total, 5);
    assert.equal(document.querySelector("[data-charts]"), charts);

    await React.act(async () => { void current!.fetchDashboardAnalytics(); });
    assert.equal(current!.analyticsError, true);
    await React.act(async () => root.render(React.createElement(Probe, { source: "local" })));
    assert.equal(current!.analyticsData, null);
    assert.equal(current!.analyticsError, false);
    assert.ok(!document.querySelector("[data-charts]"));
    assert.equal(pending[5]!.source, "local");
    await finish(5, 2);
    await finish(4, 99);
    // A Local/Web switch holds the skeleton for SWAP_MIN_MS even after the answer lands.
    assert.equal(current!.analyticsData, null);
    await React.act(async () => new Promise((resolve) => setTimeout(resolve, SWAP_MIN_MS + 50)));
    assert.equal(current!.analyticsData?.filtered_total, 2);

    await React.act(async () => root.render(React.createElement(Probe, { source: "local", range: "30d" })));
    assert.equal(current!.analyticsData, null);
    await finish(6, 1);
    assert.equal(current!.analyticsData?.filtered_total, 1);
  } finally {
    await React.act(async () => root.unmount());
    dom.window.close();
  }
});
