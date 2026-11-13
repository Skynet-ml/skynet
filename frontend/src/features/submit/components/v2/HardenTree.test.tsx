/*
 * node --test cannot load .tsx, so this runs through jiti (HardenTree.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\",\"react-loading-skeleton/dist/skeleton.css\":\"$PWD/src/features/optimizations/components/__fixtures__/style-stub.ts\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";
import type { HardeningExploit, HardeningJob, HardeningNoise, HardeningSession } from "@/shared/types/hardening";
import type { AgentToolCall } from "@/shared/ui/agent/types";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const {
  HardenTree,
  LEAD_NODE,
  bestScore,
  cardKeyStep,
  cardStep,
  focusLost,
  liveRoundIndex,
  needsReview,
  nodeStatus,
  reviewOrder,
  treeRounds,
} = await import(
  "./HardenTree"
);
const { HardenMetricScreen } = await import("./HardenMetricScreen");

// The tree takes its translator as a prop, so the test sees each key and its params.
const t = (key: string, params?: Record<string, string | number>) =>
  params ? `${key}(${Object.values(params).join("|")})` : key;

function call(tool: string, status: AgentToolCall["status"], score?: number): AgentToolCall {
  return {
    id: `${tool}-${status}`,
    tool,
    reason: "",
    status,
    startedAt: 0,
    endedAt: null,
    payload: score === undefined ? undefined : { score },
  };
}

function session(index: number, status: HardeningSession["status"], calls: AgentToolCall[] = []): HardeningSession {
  return {
    index,
    brief: `Brief ${index}`,
    status,
    transcript: [{ role: "assistant", content: "", toolCalls: calls, model: null }],
    error: null,
  };
}

function exploit(over: Partial<HardeningExploit> = {}): HardeningExploit {
  return {
    id: "e1",
    kind: "case",
    label: "exploit",
    confidence: 0.9,
    reason: "",
    unsure: false,
    patch_diff: "",
    why_md: "",
    user_verdict: null,
    session: 1,
    ...over,
  };
}

function job(over: Partial<HardeningJob> = {}): HardeningJob {
  return {
    id: "job-1",
    status: "running",
    round: 0,
    rounds: 3,
    round_cap_usd: 1,
    reserved_usd: 3,
    spent_usd: 0,
    place: "hosted",
    attacker_model: "attacker-model",
    judge_model: "judge-model",
    view: "black_box",
    preflight_failures: [],
    rounds_detail: [],
    suite_count: 0,
    fixed_count: 0,
    error: null,
    parallel_sessions: 2,
    ...over,
  } as HardeningJob;
}

void describe("treeRounds", () => {
  void it("adds the live sessions as a new round while the attack runs", () => {
    const rounds = treeRounds(job({ live_sessions: [session(1, "running"), session(2, "running")] }));
    assert.equal(rounds.length, 1);
    assert.equal(rounds[0]!.index, 1);
    assert.equal(rounds[0]!.live, true);
    assert.deepEqual(rounds[0]!.briefs, ["Brief 1", "Brief 2"]);
  });

  void it("drops the kept live sessions once the round judges, or the job stops", () => {
    const detail = [{ index: 1, status: "running", exploits: [], fix: null, sessions: [session(1, "done")] }];
    const live = [session(1, "done")];
    const judging = treeRounds(job({ round: 1, rounds_detail: detail as never, live_sessions: live }));
    assert.deepEqual(judging.map((r) => r.live), [false]);
    const stopped = treeRounds(job({ status: "clean_stop", live_sessions: live }));
    assert.equal(stopped.length, 0);
  });

  void it("keeps a round with a status the page does not know, and its lists", () => {
    const detail = [
      { index: 1, status: "failed", fix: null },
      { index: 2, status: "budget_stopped", exploits: [exploit()], fix: null, sessions: [session(1, "stopped" as never)] },
    ];
    const rounds = treeRounds(job({ status: "budget_stop", round: 2, rounds_detail: detail as never }));
    assert.deepEqual(rounds.map((r) => [r.index, r.exploits.length, r.sessions.length]), [[1, 0, 0], [2, 1, 1]]);
    // A session status the page does not know ended the session: no live pulse.
    assert.equal(nodeStatus(rounds[1]!.sessions[0]!, rounds[1]!.exploits), "found");
  });
});

void describe("liveRoundIndex", () => {
  void it("matches the tree's live round while a new round attacks", () => {
    const detail = [{ index: 1, status: "waiting_review", exploits: [], fix: null, sessions: [] }];
    const live = job({ round: 1, rounds_detail: detail as never, live_sessions: [session(1, "running")] });
    assert.equal(liveRoundIndex(live), 2);
    assert.equal(treeRounds(live).at(-1)!.index, 2);
  });

  void it("shows round 1 before the first attack, and the last round after", () => {
    assert.equal(liveRoundIndex(job({ status: "preflight" })), 1);
    const detail = [1, 2].map((index) => ({ index, status: "done", exploits: [], fix: null, sessions: [] }));
    assert.equal(liveRoundIndex(job({ status: "done", round: 2, rounds_detail: detail as never })), 2);
  });
});

void describe("cardStep", () => {
  const cards = ["a", "b", "c"].map((id) => exploit({ id }));

  void it("moves forward and back and wraps at both ends", () => {
    assert.equal(cardStep(cards, null, 1)!.id, "a");
    assert.equal(cardStep(cards, null, -1)!.id, "c");
    assert.equal(cardStep(cards, "a", 1)!.id, "b");
    assert.equal(cardStep(cards, "c", 1)!.id, "a");
    assert.equal(cardStep(cards, "a", -1)!.id, "c");
    assert.equal(cardStep(cards, "gone", 1)!.id, "a");
  });

  void it("returns null with no card to review", () => {
    assert.equal(cardStep([], null, 1), null);
  });
});

void describe("cardKeyStep", () => {
  const press = (key: string, code: string, over: Partial<KeyboardEvent> = {}, target: unknown = null) =>
    cardKeyStep({ key, code, metaKey: false, ctrlKey: false, altKey: false, ...over, target: target as EventTarget });

  void it("reads the typed letter first, and the physical key for a non-Latin layout", () => {
    assert.equal(press("j", "KeyJ"), 1);
    assert.equal(press("k", "KeyK"), -1);
    assert.equal(press("l", "KeyL"), 0);
    // Dvorak types j on the C key.
    assert.equal(press("j", "KeyC"), 1);
    // The Hebrew layout types het (U+05D7) on the J key and lamed (U+05DC) on the K key.
    // The lint rule bans Hebrew literals outside the catalogs, so build them from code points.
    assert.equal(press(String.fromCodePoint(0x05d7), "KeyJ"), 1);
    assert.equal(press(String.fromCodePoint(0x05dc), "KeyK"), -1);
    // Caps Lock.
    assert.equal(press("J", "KeyJ"), 1);
    // Colemak types n on the J key, so that is no step.
    assert.equal(press("n", "KeyJ"), 0);
  });

  void it("does nothing with a modifier or in a text field", () => {
    assert.equal(press("j", "KeyJ", { ctrlKey: true }), 0);
    assert.equal(press("j", "KeyJ", { metaKey: true }), 0);
    assert.equal(press("k", "KeyK", { altKey: true }), 0);
    assert.equal(press("j", "KeyJ", {}, { closest: () => ({}) }), 0);
    assert.equal(press("j", "KeyJ", {}, { closest: () => null }), 1);
  });
});

void describe("focusLost", () => {
  const body = { isConnected: true } as Element;

  void it("gives focus back only when the drawer took it away", () => {
    assert.equal(focusLost(null, body), true);
    assert.equal(focusLost(body, body), true);
    assert.equal(focusLost({ isConnected: false } as Element, body), true);
    // A round tab or the agent panel that holds focus keeps it.
    assert.equal(focusLost({ isConnected: true } as Element, body), false);
  });
});

void describe("HardenMetricScreen", () => {
  const state = (j: HardeningJob) => ({
    job: j,
    loading: false,
    busy: false,
    hasScorer: true,
    start: async () => {},
    verdict: async () => {},
    decideFix: async () => {},
    stop: async () => {},
    retry: async () => {},
  });
  const render = (j: HardeningJob) =>
    renderToStaticMarkup(
      <HardenMetricScreen active guided={false} onBack={() => {}} hardening={state(j)} reflectionModel={null} />,
    );

  void it("draws no tree before the first round, and draws it while the job runs", () => {
    assert.doesNotMatch(render(job({ status: "preflight" })), /role="tree"/);
    assert.doesNotMatch(render(job({ status: "preflight_failed", preflight_failures: ["no scorer"] })), /role="tree"/);
    assert.match(render(job({ status: "running" })), /role="tree"/);
  });
});

void describe("node helpers", () => {
  void it("reads the session status from its tool calls and claims", () => {
    assert.equal(nodeStatus(session(1, "running"), []), "thinking");
    assert.equal(nodeStatus(session(1, "running", [call("run_metric", "running")]), []), "scoring");
    assert.equal(nodeStatus(session(1, "done"), [exploit()]), "found");
    assert.equal(nodeStatus(session(1, "done"), []), "clean");
    assert.equal(nodeStatus(session(1, "failed"), [exploit()]), "failed");
  });

  void it("takes the best score from the claims and the finished metric runs", () => {
    const s = session(1, "running", [call("run_metric", "done", 0.4), call("run_metric", "running", 0.99)]);
    assert.equal(bestScore(s, [exploit({ exploit_score: 0.7 })]), 0.7);
    assert.equal(bestScore(s, []), 0.4);
    assert.equal(bestScore(session(1, "running"), []), null);
  });

  void it("asks for a review of open exploits and unsure claims, unsure first", () => {
    assert.equal(needsReview(exploit()), true);
    assert.equal(needsReview(exploit({ label: "valid" })), false);
    assert.equal(needsReview(exploit({ label: "valid", unsure: true })), true);
    assert.equal(needsReview(exploit({ user_verdict: "confirmed" })), false);
    const order = reviewOrder([
      exploit({ id: "valid", label: "valid" }),
      exploit({ id: "exploit" }),
      exploit({ id: "unsure", unsure: true }),
    ]);
    assert.deepEqual(order.map((e) => e.id), ["unsure", "exploit", "valid"]);
  });
});

void describe("HardenTree", () => {
  function render(noise: HardeningNoise | null = null) {
    setClientLocale("en");
    const round = {
      index: 1,
      sessions: [
        session(1, "done", [call("run_metric", "done", 0.9)]),
        session(2, "running", [call("read_score", "running")]),
      ],
      exploits: [exploit({ exploit_score: 0.9, base_score: 0.5 })],
      briefs: ["Brief 1", "Brief 2"],
      live: false,
    };
    return renderToStaticMarkup(
      <HardenTree round={round} job={job()} selected={1} noise={noise} onOpen={() => {}} t={t} />,
    );
  }

  void it("draws the lead and each session with its status, score and review badge", () => {
    const html = render();
    assert.match(html, /role="tree"/);
    assert.equal(html.match(/data-node="/g)?.length, 3);
    assert.match(html, new RegExp(`data-node="${LEAD_NODE}" tabindex="-1"`));
    assert.match(html, /data-node="1" tabindex="0"[^>]*aria-current="true"/);
    assert.match(html, /submit\.v2\.harden\.tree\.lead_desc\(2\)/);
    assert.match(html, /data-status="found"/);
    assert.match(html, /data-status="scoring"/);
    assert.equal(html.match(/submit\.v2\.harden\.node\.needs_review/g)?.length, 1);
    assert.match(html, /data-beats="true"/);
    assert.match(html, /submit\.v2\.harden\.node\.vs_base\(0\.5\)/);
    assert.match(html, /motion-safe:animate-pulse/);
    assert.match(html, /attacker-model/);
    assert.doesNotMatch(html, /noise\.title/);
  });

  void it("adds the noise node with the skip reason or the repeat scores", () => {
    const skipped = render({ skipped: true, reason: "deterministic", scores: [], spread: null });
    // The reason is user-language text, so it sits apart from the label in its own bidi island.
    assert.match(skipped, /submit\.v2\.harden\.noise\.skipped <bdi dir="auto">deterministic<\/bdi>/);
    const scored = render({ skipped: false, reason: "", scores: [0.5, 0.75], spread: 0.25 });
    assert.match(scored, /submit\.v2\.harden\.noise\.scores\(0\.5, 0\.75\|0\.25\)/);
  });
});
