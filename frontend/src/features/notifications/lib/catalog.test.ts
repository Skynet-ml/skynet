import { test } from "node:test";
import assert from "node:assert/strict";

import type { LocalNotificationChannels, NotificationItem } from "@/shared/lib/api";

import {
  CHANNELS,
  EVENT_TYPES,
  LOCAL_CHANNELS,
  appendPage,
  cellEditable,
  cellExists,
  cellShownOn,
  channelHeaderTip,
  channelHeld,
  heldChannels,
  HELD_CHANNELS,
  itemLine,
  localOffChannels,
  localOffReason,
  maybeLocalHost,
  mergeHead,
  type FactFormatters,
} from "./catalog.ts";

const fmt: FactFormatters = {
  score: (n) => n.toFixed(2),
  delta: (n) => (n > 0 ? `+${n.toFixed(2)}` : n.toFixed(2)),
  usd: (d) => `$${d}`,
  duration: (s) => `${s}s`,
};

function item(over: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: "n1",
    event_type: "finished",
    optimization_id: "opt-1",
    title: "My run",
    facts: {},
    link: "/optimizations/opt-1",
    focused: true,
    urgent: false,
    created_at: "2026-10-01T10:00:00Z",
    read_at: null,
    ...over,
  };
}

test("product news only ever has an inbox cell, and it is locked on", () => {
  assert.deepEqual(
    CHANNELS.filter((c) => cellExists("product_news", c)),
    ["inbox"],
  );
  assert.equal(cellEditable("product_news", "inbox"), false);
});

test("run events can be edited on the live channels only", () => {
  for (const event of EVENT_TYPES.filter((e) => e !== "product_news")) {
    assert.equal(cellEditable(event, "inbox"), true);
    assert.equal(cellEditable(event, "email"), true);
    assert.equal(cellEditable(event, "chrome"), true);
    assert.equal(cellEditable(event, "slack"), false);
    assert.equal(cellEditable(event, "slack", true), true);
  }
});

test("a finished line carries the score and the delta", () => {
  assert.deepEqual(itemLine(item({ facts: { score: 0.8, delta: 0.12 } }), fmt), {
    key: "notifications.item.finished_delta",
    params: { score: "0.80", delta: "+0.12" },
  });
});

test("a missing number falls back to the plain wording", () => {
  assert.deepEqual(itemLine(item({ event_type: "failed" }), fmt), {
    key: "notifications.item.failed_plain",
    params: {},
  });
  assert.deepEqual(itemLine(item({ event_type: "budget_near", facts: { spent: 4 } }), fmt), {
    key: "notifications.item.budget_near_plain",
    params: {},
  });
});

test("urgent events show spend against the limit", () => {
  assert.deepEqual(
    itemLine(item({ event_type: "needs_input", facts: { spent: 4, limit: 5 } }), fmt),
    { key: "notifications.item.needs_input", params: { spent: "$4", limit: "$5" } },
  );
});

test("a share line names who shared and the kind of share", () => {
  assert.deepEqual(
    itemLine(item({ event_type: "shared", facts: { actor: "dana", kind: "role" } }), fmt),
    { key: "notifications.item.shared_role", params: { actor: "dana" } },
  );
});

test("a hardening job asks for review and reports when it is done", () => {
  const hardening = { kind: "hardening", spent: 4, limit: 5, score: 0.8 };
  assert.deepEqual(itemLine(item({ event_type: "needs_input", facts: hardening }), fmt), {
    key: "notifications.item.hardening_review",
    params: {},
  });
  assert.deepEqual(itemLine(item({ event_type: "finished", facts: hardening }), fmt), {
    key: "notifications.item.hardening_done",
    params: {},
  });
  assert.deepEqual(itemLine(item({ event_type: "budget_near", facts: hardening }), fmt), {
    key: "notifications.item.budget_near",
    params: { spent: "$4", limit: "$5" },
  });
});

test("news uses its headline as written", () => {
  assert.deepEqual(itemLine(item({ event_type: "product_news", title: "Inbox" }), fmt), {
    text: "Inbox",
  });
});

test("an older page never repeats items already shown", () => {
  const a = item({ id: "a" });
  const b = item({ id: "b" });
  assert.deepEqual(
    appendPage([a], [a, b]).map((i) => i.id),
    ["a", "b"],
  );
});

test("a refreshed head keeps older items loaded by scrolling", () => {
  const old = item({ id: "old", created_at: "2026-09-01T00:00:00Z" });
  const head = [
    item({ id: "new", created_at: "2026-10-02T00:00:00Z" }),
    item({ id: "mid", created_at: "2026-10-01T00:00:00Z" }),
  ];
  assert.deepEqual(
    mergeHead([head[1]!, old], head).map((i) => i.id),
    ["new", "mid", "old"],
  );
});

function localState(
  over: Partial<LocalNotificationChannels["channels"]> = {},
): LocalNotificationChannels {
  return {
    channels: { email: false, chrome: false, slack: false, macos: true, ...over },
    macos: Object.fromEntries(
      EVENT_TYPES.map((e) => [e, false]),
    ) as LocalNotificationChannels["macos"],
    macos_held: false,
  };
}

test("the Mac app keeps every hosted column and adds macOS last", () => {
  assert.deepEqual(LOCAL_CHANNELS, [...CHANNELS, "macos"]);
});

test("local provider credentials do not disable hosted account channels", () => {
  const local = localState();
  assert.equal(localOffReason("email", local), null);
  assert.equal(localOffReason("chrome", local), null);
  assert.equal(localOffReason("slack", local), null);
  assert.equal(localOffReason("macos", local), null);
  assert.equal(localOffReason("inbox", local), null);
  assert.equal(cellEditable("finished", "email", true, local), true);
  assert.equal(cellEditable("finished", "slack", true, local), true);
  assert.equal(cellEditable("finished", "slack", false, local), false);
  assert.equal(cellEditable("finished", "macos", false, local), true);
  assert.equal(cellEditable("product_news", "macos", false, local), false);
});

test("in the Mac app a hosted channel set up on this Mac stays editable", () => {
  const local = localState({ email: true });
  assert.equal(localOffReason("email", local), null);
  assert.equal(cellEditable("finished", "email", false, local), true);
});

test("on a hosted server nothing is reported off by the local rules", () => {
  assert.equal(localOffReason("email", null), null);
  assert.equal(cellEditable("finished", "slack", false, null), false);
});

test("only loopback pages probe the local-only routes", () => {
  assert.equal(maybeLocalHost("127.0.0.1"), true);
  assert.equal(maybeLocalHost("localhost"), true);
  assert.equal(maybeLocalHost("[::1]"), true);
  assert.equal(maybeLocalHost("app.example.com"), false);
});

test("the Mac app preserves hosted preferences without local provider setup", () => {
  const local = localState({ email: true });
  assert.equal(cellShownOn(true, "chrome", local), true);
  assert.equal(cellShownOn(true, "email", local), true);
  assert.equal(cellShownOn(false, "email", local), false);
  assert.equal(cellShownOn(true, "inbox", local), true);
  assert.equal(cellShownOn(true, "slack", null), true);
});

test("only unavailable native delivery appears in the local setup warning", () => {
  assert.deepEqual(localOffChannels(localState({ email: true })), []);
  assert.deepEqual(localOffChannels(localState({ macos: false })), ["macos"]);
  assert.equal(cellEditable("finished", "macos", true, localState({ macos: false })), false);
  assert.equal(cellShownOn(true, "macos", localState({ macos: false })), false);
  assert.deepEqual(localOffChannels(localState({ email: true, chrome: true, slack: true })), []);
  assert.deepEqual(localOffChannels(null), []);
});

test("the hold covers macOS only in the Mac app", () => {
  assert.deepEqual(heldChannels(null), HELD_CHANNELS);
  assert.ok(!heldChannels(null).includes("macos"));
  assert.deepEqual(heldChannels(localState()), [...HELD_CHANNELS, "macos"]);
});

test("the macOS hold comes from the Mac app's state, the others from the preferences", () => {
  const local = { ...localState(), macos_held: true };
  assert.equal(channelHeld("macos", [], local), true);
  assert.equal(channelHeld("macos", [], localState()), false);
  assert.equal(channelHeld("macos", [], null), false);
  assert.equal(channelHeld("email", ["email"], local), true);
  assert.equal(channelHeld("chrome", ["email"], local), false);
  assert.equal(channelHeld("inbox", ["email"], local), false);
});

test("a hosted header keeps its own Slack wording, without the channel prefix", () => {
  assert.deepEqual(channelHeaderTip("slack", false, null), ["notifications.slack.unavailable"]);
  assert.deepEqual(channelHeaderTip("slack", true, null), ["notifications.channel.slack"]);
  assert.deepEqual(channelHeaderTip("email", false, null), ["notifications.channel.email"]);
});

test("a Mac app header uses hosted Slack status and local native availability", () => {
  assert.deepEqual(channelHeaderTip("slack", false, localState()), [
    "notifications.slack.unavailable",
  ]);
  assert.deepEqual(channelHeaderTip("macos", false, localState()), ["notifications.channel.macos"]);
});
