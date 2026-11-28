import { test } from "node:test";
import assert from "node:assert/strict";

// Node has EventTarget and CustomEvent; a bare one stands in for window.
(globalThis as { window?: unknown }).window = new EventTarget();

const {
  SIDEBAR_STATE_EVENT,
  SIDEBAR_TOGGLE_EVENT,
  announceSidebarState,
  lastSidebarCollapsed,
  requestSidebarToggle,
} = await import("./sidebar-toggle.ts");

void test("a toggle request gets the next state back, and the last state is stored", () => {
  // Stand in for the sidebar: flip the flag on each request and announce it.
  let collapsed = false;
  const onToggle = () => {
    collapsed = !collapsed;
    announceSidebarState(collapsed);
  };
  const seen: boolean[] = [];
  const onState = (e: Event) => seen.push((e as CustomEvent<{ collapsed: boolean }>).detail.collapsed);
  window.addEventListener(SIDEBAR_TOGGLE_EVENT, onToggle);
  window.addEventListener(SIDEBAR_STATE_EVENT, onState);
  try {
    assert.equal(lastSidebarCollapsed(), false);
    requestSidebarToggle();
    assert.deepEqual(seen, [true]);
    assert.equal(lastSidebarCollapsed(), true);
    requestSidebarToggle();
    assert.deepEqual(seen, [true, false]);
    assert.equal(lastSidebarCollapsed(), false);
  } finally {
    window.removeEventListener(SIDEBAR_TOGGLE_EVENT, onToggle);
    window.removeEventListener(SIDEBAR_STATE_EVENT, onState);
  }
});
