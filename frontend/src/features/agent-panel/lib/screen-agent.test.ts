import assert from "node:assert/strict";
import { test } from "node:test";

import { registerHooks } from "node:module";

// screen-agent.ts imports its neighbours the way the app's bundler resolves
// them; the plain node runner needs the file paths.
const screenAgentUrl = new URL("./screen-agent.ts", import.meta.url).href;
const srcUrl = new URL("../../../", import.meta.url);
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (context.parentURL === screenAgentUrl && !specifier.endsWith(".ts")) {
      const url = specifier.startsWith("@/")
        ? new URL(`${specifier.slice(2)}.ts`, srcUrl)
        : new URL(`${specifier}.ts`, screenAgentUrl);
      return { shortCircuit: true, url: url.href };
    }
    return nextResolve(specifier, context);
  },
});

const {
  autoFeedbackAction,
  decodeSetupChat,
  encodeSetupChat,
  flushSetupChat,
  legacySetupChatId,
  newSetupChatId,
  nextReturnScreen,
  reachedReturnScreen,
  opensWithKickoff,
  parseScreenNav,
  registerSetupChatFlush,
  runScreenNav,
  SETUP_CHAT_MAX_CHARS,
  setupChatSnapshot,
  storedKickedScreens,
  trackAutoTurn,
  trackKickoffTurn,
} = await import("./screen-agent.ts");

test("a continue result runs the Continue button's own handler", () => {
  const calls: string[] = [];
  const navigator = {
    screen: "brief",
    forward: () => calls.push("forward"),
    goToScreen: (s: string) => calls.push(`go:${s}`),
    goForwardTo: (s: string) => calls.push(`to:${s}`),
  };
  const nav = parseScreenNav({ screen_nav: { action: "continue", from: "brief" } });
  assert.deepEqual(nav, { action: "continue", from: "brief" });
  runScreenNav(nav!, navigator);
  assert.deepEqual(calls, ["forward"]);
});

test("a continue with a return screen jumps straight back there", () => {
  const calls: string[] = [];
  const navigator = {
    screen: "optimizer",
    forward: () => calls.push("forward"),
    goToScreen: (s: string) => calls.push(`go:${s}`),
    goForwardTo: (s: string) => calls.push(`to:${s}`),
  };
  const nav = parseScreenNav({ screen_nav: { action: "continue", from: "optimizer", to: "budget" } });
  assert.deepEqual(nav, { action: "continue", from: "optimizer", to: "budget" });
  runScreenNav(nav!, navigator);
  assert.deepEqual(calls, ["to:budget"]);
});

test("the return screen is the latest screen a go-back left, until the wizard reaches it", () => {
  const back = (from: string) => ({ action: "back" as const, from, screen: "brief", request: "" });
  assert.equal(nextReturnScreen(null, back("budget")), "budget");
  assert.equal(nextReturnScreen("budget", back("optimizer")), "budget");
  assert.equal(nextReturnScreen("budget", { action: "continue", from: "brief" }), "budget");
  // A held jump keeps the screen; the wizard clears it when it gets there.
  assert.equal(nextReturnScreen("budget", { action: "continue", from: "optimizer", to: "budget" }), "budget");
  // A go-back from past the old screen means the user moved on: the newer screen wins.
  assert.equal(nextReturnScreen("budget", back("overview")), "overview");
  // The user moved on to metric while the agent's turn from budget ran.
  assert.equal(nextReturnScreen(null, back("budget"), "metric"), "metric");
});

test("the wizard reaches the return screen on it or past it", () => {
  assert.equal(reachedReturnScreen("budget", "budget"), true);
  assert.equal(reachedReturnScreen("budget", "metric"), true);
  assert.equal(reachedReturnScreen("budget", "brief"), false);
  assert.equal(reachedReturnScreen("budget", null), false);
});

test("a back result jumps to the named screen with its request", () => {
  const calls: string[] = [];
  const navigator = {
    screen: "budget",
    forward: () => calls.push("forward"),
    goToScreen: (s: string) => calls.push(`go:${s}`),
    goForwardTo: (s: string) => calls.push(`to:${s}`),
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
    goForwardTo: (s: string) => calls.push(`to:${s}`),
  };
  runScreenNav({ action: "continue", from: "brief" }, navigator);
  assert.deepEqual(calls, []);
});

test("the setup chat keeps its id, the screens already started and its messages", () => {
  const messages = [
    { role: "user" as const, content: "hi", hidden: true },
    { role: "assistant" as const, content: "hello", toolCalls: [] },
  ];
  const raw = encodeSetupChat({ id: "c1", kicked: ["brief", "source"], messages });
  assert.deepEqual(decodeSetupChat(raw), { id: "c1", kicked: ["brief", "source"], messages });
  assert.equal(decodeSetupChat(null), null);
  assert.equal(decodeSetupChat("not json"), null);
  assert.equal(decodeSetupChat('{"kicked":[],"messages":[]}'), null);
  assert.deepEqual(
    decodeSetupChat('{"id":"c2","kicked":[1,"brief"],"messages":[{"role":"user","content":"a"},{"role":"x"},null]}'),
    { id: "c2", kicked: ["brief"], messages: [{ role: "user", content: "a" }] },
  );
});

test("a setup chat kept with no messages, as the server once saved it, is not reopened", () => {
  const old = '{"id":"c3","kicked":["brief"]}';
  assert.equal(decodeSetupChat(old), null);
  assert.equal(legacySetupChatId(old), "c3");
  assert.equal(legacySetupChatId('{"id":"c4","kicked":[],"messages":[]}'), null);
  assert.equal(legacySetupChatId(null), null);
  assert.equal(legacySetupChatId("not json"), null);
});

test("a mid-turn copy keeps the user's message and the reply so far, without an empty bubble", () => {
  const user = { role: "user" as const, content: "q" };
  const empty = { role: "assistant" as const, content: "", toolCalls: [] };
  const partial = { role: "assistant" as const, content: "half" };
  assert.deepEqual(setupChatSnapshot([user, empty], null, 5), [user]);
  assert.deepEqual(setupChatSnapshot([user, partial], null, 5), [user, partial]);
});

test("a mid-turn copy ends running and pending tool rows and open thinking", () => {
  const user = { role: "user" as const, content: "set it" };
  const running = {
    id: "t1", tool: "update_wizard_state", reason: "", status: "running" as const,
    startedAt: 1, endedAt: null, payload: { arguments: { objective: "x" } },
  };
  const pending = { id: "pending:0", tool: "x", reason: "", status: "running" as const, startedAt: 1, endedAt: null };
  const done = { ...running, id: "t0", status: "done" as const, endedAt: 2 };
  const reply = {
    role: "assistant" as const, content: "",
    toolCalls: [done, running, pending],
    thinkingSegments: [{ reasoning: "hm", toolsBefore: 0, startedAt: 1, endedAt: null }],
  };
  const [, kept] = setupChatSnapshot([user, reply], null, 9);
  assert.deepEqual(kept?.toolCalls, [done, { ...running, status: "error", endedAt: 9 }]);
  assert.equal(kept?.thinkingSegments?.[0]?.endedAt, 9);
  const onlyPending = { role: "assistant" as const, content: "", toolCalls: [pending] };
  assert.deepEqual(setupChatSnapshot([user, onlyPending], null, 9), [user]);
});

test("a mid-turn copy drops a running kickoff but keeps what the user wrote during it", () => {
  const earlier = { role: "assistant" as const, content: "done" };
  const kickoff = { role: "user" as const, content: "kick", hidden: true };
  const reply = { role: "assistant" as const, content: "half" };
  const steer = { role: "user" as const, content: "use refunds" };
  assert.deepEqual(setupChatSnapshot([earlier, kickoff, reply], 1, 5), [earlier]);
  assert.deepEqual(setupChatSnapshot([earlier, kickoff, reply, steer, reply], 1, 5), [earlier, steer]);
});

test("a kickoff turn ends only after the agent started on it", () => {
  const sent = { screen: "brief", from: 2, started: false };
  assert.equal(trackKickoffTurn(null, true), null);
  assert.deepEqual(trackKickoffTurn(sent, false), sent);
  const running = trackKickoffTurn(sent, true);
  assert.deepEqual(running, { ...sent, started: true });
  assert.equal(trackKickoffTurn(running, true), running);
  assert.equal(trackKickoffTurn(running, false), null);
});

test("the tab's copy counts a screen as started only once its kickoff ended", () => {
  const kicked = new Set(["brief", "source"]);
  assert.deepEqual(storedKickedScreens(kicked, { screen: "source", from: 0, started: true }), ["brief"]);
  assert.deepEqual(storedKickedScreens(kicked, null), ["brief", "source"]);
});

test("the wizard's flush runs the panel's writer until the panel removes it", () => {
  let runs = 0;
  const writer = () => {
    runs++;
  };
  const remove = registerSetupChatFlush(writer);
  flushSetupChat();
  assert.equal(runs, 1);
  const other = registerSetupChatFlush(() => {});
  remove();
  flushSetupChat();
  assert.equal(runs, 1);
  other();
  flushSetupChat();
});

test("a setup chat id is the browser's own", () => {
  const a = newSetupChatId();
  assert.match(a, /^setup-/);
  assert.notEqual(a, newSetupChatId());
});

test("a setup chat too long for storage loses tool results, then its oldest messages", () => {
  const big = "x".repeat(SETUP_CHAT_MAX_CHARS / 4);
  const call = { id: "t", tool: "read", reason: "", status: "done" as const, startedAt: 0, endedAt: 1 };
  const withResults = [
    { role: "user" as const, content: "q" },
    { role: "assistant" as const, content: "a", toolCalls: [{ ...call, payload: { result: big.repeat(5) } }] },
  ];
  const trimmed = decodeSetupChat(encodeSetupChat({ id: "c", kicked: [], messages: withResults }));
  assert.deepEqual(trimmed?.messages, [withResults[0], { role: "assistant", content: "a", toolCalls: [call] }]);

  const long = Array.from({ length: 6 }, (_, i) => ({ role: "user" as const, content: `${i}${big}` }));
  const raw = encodeSetupChat({ id: "c", kicked: [], messages: long });
  assert.ok(raw.length <= SETUP_CHAT_MAX_CHARS);
  const kept = decodeSetupChat(raw)?.messages ?? [];
  assert.equal(kept.length, 3);
  assert.deepEqual(kept, long.slice(3));
});

test("trimming a long setup chat keeps the longest tail that fits, in one pass", () => {
  const messages = Array.from({ length: 600 }, (_, i) => ({
    role: i % 2 ? ("assistant" as const) : ("user" as const),
    content: `${i} ${"y".repeat(1000)}`,
  }));
  const started = performance.now();
  const raw = encodeSetupChat({ id: "c", kicked: ["brief"], messages });
  assert.ok(performance.now() - started < 200);
  assert.ok(raw.length <= SETUP_CHAT_MAX_CHARS);
  const kept = decodeSetupChat(raw)?.messages ?? [];
  assert.deepEqual(kept, messages.slice(messages.length - kept.length));
  const oneMore = messages.slice(messages.length - kept.length - 1);
  assert.ok(JSON.stringify({ id: "c", kicked: ["brief"], messages: oneMore }).length > SETUP_CHAT_MAX_CHARS);
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

test("the Check screen opens with no kickoff turn; other screens do", () => {
  assert.equal(opensWithKickoff("check"), false);
  for (const screen of ["brief", "inputs", "optimizer", "metric", "overview"]) {
    assert.equal(opensWithKickoff(screen), true);
  }
});
