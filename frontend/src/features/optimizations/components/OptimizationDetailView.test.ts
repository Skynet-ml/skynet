import assert from "node:assert/strict";
import { Module, registerHooks } from "node:module";
import path from "node:path";
import { after, describe, it } from "node:test";
import * as React from "react";
import { createJiti } from "jiti";
import { JSDOM } from "jsdom";

// The view pulls in a skeleton stylesheet that Node cannot load.
registerHooks({
  load: (url, context, next) =>
    url.endsWith(".css")
      ? { format: "module", source: "", shortCircuit: true }
      : next(url, context),
});
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
Object.assign(globalThis, {
  React,
  window: dom.window,
  document: dom.window.document,
  self: dom.window,
  HTMLElement: dom.window.HTMLElement,
  customElements: dom.window.customElements,
  SVGElement: dom.window.SVGElement,
  KeyboardEvent: dom.window.KeyboardEvent,
  MutationObserver: dom.window.MutationObserver,
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  MouseEvent: dom.window.MouseEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
});
const matchMedia = (media: string) => ({
  matches: false,
  media,
  onchange: null,
  addListener() {},
  removeListener() {},
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent: () => true,
});
Object.assign(dom.window, { matchMedia });
Object.assign(globalThis, { matchMedia });
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
after(() => dom.window.close());
const jiti = createJiti(import.meta.url, {
  jsx: true,
  alias: { "@/": `${path.join(process.cwd(), "src")}/` },
});

function stub(id: string, exports: Record<string, unknown>) {
  const resolved = jiti.resolve(id);
  const module = new Module(resolved);
  module.exports = exports;
  module.loaded = true;
  jiti.cache[resolved] = module;
}

stub("next/navigation", {
  useParams: () => ({}),
  useRouter: () => ({ push: () => {} }),
  useSearchParams: () => new URLSearchParams(),
});
stub("next-auth/react", { useSession: () => ({ data: null, status: "authenticated" }) });
const calls: string[] = [];
stub("@/shared/lib/api", {
  ...(await jiti.import<Record<string, unknown>>("@/shared/lib/api")),
  cancelJob: (id: string, source: string) => {
    calls.push(`cancel ${id} ${source}`);
    return Promise.resolve();
  },
  pauseJob: (id: string, source: string) => {
    calls.push(`pause ${id} ${source}`);
    return Promise.resolve();
  },
});

type Job = {
  status: string;
  stop_reason?: string | null;
  effective_role?: string | null;
  pausable?: boolean;
  resumable?: boolean;
};
type Actions = {
  canCancel: boolean;
  canPause: boolean;
  headerActionCounts: { wide: number | null; phone: number | null };
};
const { CancelRunButton, runHeaderActions } = await jiti.import<{
  CancelRunButton: React.ComponentType<{ id: string; source: "local"; onCancelled: () => void }>;
  runHeaderActions: (args: {
    job: Job;
    source: "local";
    isShare: boolean;
    shareCanInteract: boolean;
  }) => Actions;
}>("./OptimizationDetailView");

void describe("run page header actions (SKY-827)", () => {
  // wide counts on the owner's local run: share + clone + restart + pause + cancel + delete.
  const cases: [
    string,
    Job,
    boolean,
    Partial<Actions> & { wide: number | null; phone: number | null },
  ][] = [
    [
      "running",
      { status: "running", pausable: true },
      false,
      { canCancel: true, canPause: true, wide: 4, phone: 2 },
    ],
    [
      "pending",
      { status: "pending", pausable: false },
      false,
      { canCancel: true, canPause: false, wide: 3, phone: 2 },
    ],
    [
      "manual pause",
      { status: "paused", stop_reason: "user_paused", pausable: true, resumable: true },
      false,
      { canCancel: true, canPause: false, wide: 5, phone: 2 },
    ],
    [
      "budget pause",
      { status: "paused", stop_reason: "budget_projected", pausable: true, resumable: true },
      false,
      { canCancel: true, canPause: false, wide: 4, phone: 2 },
    ],
    [
      "viewer on a paused run",
      { status: "paused", effective_role: "viewer", pausable: true, resumable: true },
      false,
      { canCancel: false, canPause: false, wide: 1, phone: 0 },
    ],
    [
      "cancelled",
      { status: "cancelled", pausable: true, resumable: false },
      false,
      { canCancel: false, canPause: false, wide: 4, phone: 1 },
    ],
    [
      "share view of a paused run",
      { status: "paused", pausable: true, resumable: true },
      true,
      { wide: 1, phone: null },
    ],
  ];
  for (const [name, job, isShare, want] of cases) {
    void it(name, () => {
      const got = runHeaderActions({ job, source: "local", isShare, shareCanInteract: isShare });
      if (want.canCancel !== undefined) assert.equal(got.canCancel, want.canCancel, "canCancel");
      if (want.canPause !== undefined) assert.equal(got.canPause, want.canPause, "canPause");
      assert.deepEqual(got.headerActionCounts, { wide: want.wide, phone: want.phone });
    });
  }
});

void it("the Cancel button cancels the run and tells the sidebar (SKY-827)", async () => {
  const { act } = await import("react");
  const { createRoot } = await import("react-dom/client");
  window.addEventListener("optimizations-changed", () => calls.push("optimizations-changed"));
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(
      React.createElement(CancelRunButton, {
        id: "run-1",
        source: "local",
        onCancelled: () => calls.push("refetch"),
      }),
    );
  });
  const button = host.querySelector("button");
  assert.ok(button);
  await act(async () => {
    button.click();
  });
  assert.deepEqual(calls, ["cancel run-1 local", "optimizations-changed", "refetch"]);
  await act(async () => root.unmount());
});
