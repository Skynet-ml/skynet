import assert from "node:assert/strict";
import { test } from "node:test";

import {
  autoFeedbackAction,
  decodeSetupChat,
  encodeSetupChat,
  parseScreenNav,
  runScreenNav,
  trackAutoTurn,
} from "./screen-agent.ts";

test("a continue result runs the Continue button's own handler", () => {
  const calls: string[] = [];
  const navigator = {
    screen: "brief",
    forward: () => calls.push("forward"),
    goToScreen: (s: string) => calls.push(`go:${s}`),
  };
  const nav = parseScreenNav({ screen_nav: { action: "continue", from: "brief" } });
  assert.deepEqual(nav, { action: "continue", from: "brief" });
  runScreenNav(nav!, navigator);
  assert.deepEqual(calls, ["forward"]);
});

test("a back result jumps to the named screen with its request", () => {
  const calls: string[] = [];
  const navigator = {
    screen: "budget",
    forward: () => calls.push("forward"),
    goToScreen: (s: string) => calls.push(`go:${s}`),
  };
  const nav = parseScreenNav({
    screen_nav: { action: "back", from: "budget", screen: "brief", request: "refunds" },
  });
  assert.deepEqual(nav, { action: "back", from: "budget", screen: "brief", request: "refunds" });
  runScreenNav(nav!, navigator);
  assert.deepEqual(calls, ["go:brief"]);
});

test("results without a move do nothing", () => {
  assert.equal(parseScreenNav({ wizard_state: { screen_notes: [] } }), null);
  assert.equal(parseScreenNav("Can't continue yet"), null);
  assert.equal(parseScreenNav({ screen_nav: { action: "back", screen: "" } }), null);
  runScreenNav({ action: "continue", from: "brief" }, null);
});

test("a continue from a screen the wizard already left is dropped", () => {
  const calls: string[] = [];
  const navigator = {
    screen: "artifact",
    forward: () => calls.push("forward"),
    goToScreen: (s: string) => calls.push(`go:${s}`),
  };
  runScreenNav({ action: "continue", from: "brief" }, navigator);
  assert.deepEqual(calls, []);
});

test("the setup chat keeps its id and the screens already started", () => {
  const raw = encodeSetupChat({ id: "c1", kicked: ["brief", "source"] });
  assert.deepEqual(decodeSetupChat(raw), { id: "c1", kicked: ["brief", "source"] });
  assert.equal(decodeSetupChat(null), null);
  assert.equal(decodeSetupChat("not json"), null);
  assert.equal(decodeSetupChat('{"kicked":[]}'), null);
  assert.deepEqual(decodeSetupChat('{"id":"c2","kicked":[1,"brief"]}'), { id: "c2", kicked: ["brief"] });
});

test("an automatic message waits for the chat on view and a free agent, and a silent clone drops it", () => {
  assert.equal(autoFeedbackAction({ docked: true, busy: false, silent: false }), "send");
  assert.equal(autoFeedbackAction({ docked: false, busy: false, silent: false }), "wait");
  assert.equal(autoFeedbackAction({ docked: true, busy: true, silent: false }), "wait");
  assert.equal(autoFeedbackAction({ docked: true, busy: false, silent: true }), "skip");
});

test("an automatic turn ends only after the agent started on it", () => {
  let step = trackAutoTurn({ id: 4, started: false }, false);
  assert.deepEqual(step, { turn: { id: 4, started: false }, ended: null });
  step = trackAutoTurn(step.turn, true);
  assert.deepEqual(step, { turn: { id: 4, started: true }, ended: null });
  step = trackAutoTurn(step.turn, false);
  assert.deepEqual(step, { turn: null, ended: 4 });
  assert.deepEqual(trackAutoTurn(null, false), { turn: null, ended: null });
});
