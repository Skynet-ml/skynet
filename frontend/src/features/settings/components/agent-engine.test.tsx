/*
 * node --test cannot load .tsx, so this runs through jiti (agent-engine.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";
import type { AgentEngine, CliStatus, LocalStatus } from "@/features/local-runtime";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { AgentEngineSelect, AgentEngineSetting, EngineChoice, engineToSave } = await import(
  "./AgentEngineSetting"
);
const { Select } = await import("@/shared/ui/primitives/select");
const {
  agentEngineLoaded,
  agentEngineOptions,
  loadAgentEngine,
  loadLocalStatus,
  localAgentModel,
  peekAgentEngine,
  peekAgentEngineStored,
  saveAgentEngine,
} = await import("@/features/local-runtime");

const cli = (id: CliStatus["id"], patch: Partial<CliStatus> = {}): CliStatus => ({
  id,
  path: `/usr/local/bin/${id}`,
  version: "2.1.0",
  min_version: "2.0.0",
  version_ok: true,
  logged_in: true,
  login_hint: "Run it in a terminal.",
  rate_limited_until: null,
  models: ["openai/gpt-5"],
  ...patch,
});

const status = (clis: CliStatus[]): LocalStatus => ({
  local_mode: true,
  version: "0.1.0",
  docker: { available: true, error: null },
  sandbox_image: "skynet-sandbox:latest",
  clis,
});

function render(
  value: AgentEngine,
  s: LocalStatus | null,
  keyVerified: boolean | null = false,
  automatic = false,
): string {
  setClientLocale("en");
  return renderToStaticMarkup(
    <AgentEngineSelect
      status={s}
      value={value}
      keyVerified={keyVerified}
      automatic={automatic}
      saving={false}
      onChange={() => {}}
    />,
  );
}

// Runs first: later tests load and save the engine into the shared module cache.
void describe("agent engine load state (SKY-123 R11-1)", () => {
  // First in the file: no test has loaded or saved an engine yet.
  void it("an engine not loaded yet is not shown as automatic", () => {
    const realLocal = process.env.APP_LOCAL_MODE;
    process.env.APP_LOCAL_MODE = "1";
    try {
      assert.equal(peekAgentEngineStored(), null);
    } finally {
      if (realLocal === undefined) delete process.env.APP_LOCAL_MODE;
      else process.env.APP_LOCAL_MODE = realLocal;
    }
  });

  void it("a failed first load leaves the engine unknown, not a CLI engine; a later load settles it", async () => {
    const realFetch = globalThis.fetch;
    const realLocal = process.env.APP_LOCAL_MODE;
    process.env.APP_LOCAL_MODE = "1";
    let fails = true;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify(fails ? { detail: "boom" } : { engine: "openrouter" }), {
        status: fails ? 500 : 200,
        headers: { "Content-Type": "application/json" },
      })) as typeof fetch;
    try {
      // Before any load, the engine is unknown.
      assert.equal(agentEngineLoaded(), false);
      assert.equal(await loadAgentEngine(), null);
      assert.equal(agentEngineLoaded(), false);
      // The empty menu keeps a key pick on the chip, so the request sends it unchanged.
      const pick = "openrouter/anthropic/claude-sonnet-4.5";
      assert.equal(await localAgentModel(pick), pick);
      assert.equal(await localAgentModel(null), null);
      fails = false;
      assert.equal(await loadAgentEngine(), "openrouter");
      assert.equal(agentEngineLoaded(), true);
      assert.equal(peekAgentEngine(), "openrouter");
    } finally {
      globalThis.fetch = realFetch;
      if (realLocal === undefined) delete process.env.APP_LOCAL_MODE;
      else process.env.APP_LOCAL_MODE = realLocal;
    }
  });
});

void describe("AgentEngineSelect", () => {
  void it("shows only in the local build, not in the hosted web app", () => {
    const realLocal = process.env.APP_LOCAL_MODE;
    try {
      delete process.env.APP_LOCAL_MODE;
      assert.equal(AgentEngineSetting(), null);
      process.env.APP_LOCAL_MODE = "1";
      assert.notEqual(AgentEngineSetting(), null);
    } finally {
      if (realLocal === undefined) delete process.env.APP_LOCAL_MODE;
      else process.env.APP_LOCAL_MODE = realLocal;
    }
  });

  void it("a ready local engine shows the CLI and no warning", () => {
    const html = render("codex", status([cli("codex")]));
    assert.match(html, /subscription\.cli\.codex/);
    assert.match(html, /settings\.agent_engine\.description_local/);
    assert.doesNotMatch(html, /role="status"/);
  });

  void it("an engine the user never saved shows as Automatic, with the engine it runs on", () => {
    const html = render("claude", status([cli("claude"), cli("codex")]), false, true);
    assert.match(html, /settings\.agent_engine\.automatic/);
    assert.match(html, /data-engine-logo="claude"/);
    assert.doesNotMatch(render("claude", status([cli("claude")])), /agent_engine\.automatic/);
  });

  void it("choosing the shown engine saves it only while it is automatic", () => {
    // Radix Select reports no change for the current value, so automatic uses its own value.
    assert.equal(engineToSave("claude", "claude", true), "claude");
    assert.equal(engineToSave("claude", "claude", false), null);
    assert.equal(engineToSave("codex", "claude", false), "codex");
    assert.equal(engineToSave("automatic", "claude", true), null);
  });

  void it("a signed-out local engine says why, with its sign-in command", () => {
    const html = render("codex", status([cli("codex", { logged_in: false })]));
    assert.match(html, /role="status"/);
    assert.match(html, /subscription\.state\.logged_out/);
    assert.match(html, /<code[^>]*>codex login<\/code>/);
  });
});

// Each engine's mark, by its accessible name.
const BRAND: Record<AgentEngine, RegExp[]> = {
  codex: [/aria-label="Codex"/],
  claude: [/aria-label="Claude Code"/],
  openrouter: [/aria-label="OpenRouter"/],
};

function assertLogo(html: string, engine: AgentEngine) {
  // The logo is decorative: hidden from assistive tech, so the name stays the label.
  assert.match(html, new RegExp(`<span aria-hidden="true" data-engine-logo="${engine}"`));
  for (const brand of BRAND[engine]) assert.match(html, brand);
}

void describe("engine logos", () => {
  void it("the selected value shows the engine's logo next to its name", () => {
    for (const engine of ["codex", "claude"] as const) {
      const html = render(engine, status([cli("codex"), cli("claude")]));
      assertLogo(html, engine);
      assert.match(html, new RegExp(`subscription\\.cli\\.${engine}`));
    }
  });

  void it("every menu choice shows its logo, also when it is disabled", () => {
    setClientLocale("en");
    const s = status([cli("codex"), cli("claude", { logged_in: false })]);
    const options = agentEngineOptions(s, true);
    assert.deepEqual(
      options.map((o) => o.engine),
      ["codex", "claude", "openrouter"],
    );
    for (const option of options) {
      const html = renderToStaticMarkup(<EngineChoice option={option} status={s} failed={false} />);
      assertLogo(html, option.engine);
      if (option.engine === "claude") {
        assert.equal(option.disabled, true);
        assert.match(html, /subscription\.state\.logged_out/);
        assert.match(html, /<code[^>]*>claude auth login<\/code>/);
      }
    }
  });

  void it("offers no Droid choice, even when an older backend reports it", () => {
    setClientLocale("en");
    const droid = { ...cli("codex"), id: "droid" } as unknown as CliStatus;
    const s = status([cli("codex"), cli("claude"), droid]);
    assert.deepEqual(
      agentEngineOptions(s, true).map((o) => o.engine),
      ["codex", "claude", "openrouter"],
    );
    assert.doesNotMatch(render("codex", s), /droid/i);
  });
});

void describe("saveAgentEngine", () => {
  void it("sends PUT /account/agent-engine and returns the stored engine", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    const realFetch = globalThis.fetch;
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      return new Response(JSON.stringify({ engine: "codex" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;
    try {
      assert.equal(await saveAgentEngine("codex"), "codex");
    } finally {
      globalThis.fetch = realFetch;
    }
    assert.equal(calls.length, 1);
    assert.match(calls[0]!.url, /\/account\/agent-engine$/);
    assert.equal(calls[0]!.init?.method, "PUT");
    assert.deepEqual(JSON.parse(String(calls[0]!.init?.body)), { engine: "codex" });
  });

  void it("throws the backend's refusal so the caller can show it", async () => {
    const realFetch = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ code: "models.subscription_local_only" }), {
        status: 422,
        headers: { "Content-Type": "application/json" },
      })) as typeof fetch;
    try {
      await assert.rejects(saveAgentEngine("codex"), (err: unknown) => {
        assert.equal((err as { code?: string }).code, "models.subscription_local_only");
        assert.equal((err as { status?: number }).status, 422);
        return true;
      });
    } finally {
      globalThis.fetch = realFetch;
    }
  });
});

void describe("loadAgentEngine", () => {
  void it("an unchosen engine follows the backend when the CLI status refreshes", async () => {
    // The backend's engine with nothing stored is the first ready CLI.
    let backendEngine = "codex";
    const realFetch = globalThis.fetch;
    const realLocal = process.env.APP_LOCAL_MODE;
    process.env.APP_LOCAL_MODE = "1";
    globalThis.fetch = (async (url: string) => {
      const body = String(url).endsWith("/account/agent-engine")
        ? { engine: backendEngine }
        : status([cli("codex"), cli("claude")]);
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;
    try {
      await loadLocalStatus(true);
      assert.equal(await loadAgentEngine(), "codex");
      // Codex signs out or hits its cap, so the backend now picks Claude.
      backendEngine = "claude";
      await loadLocalStatus(true);
      assert.equal(await loadAgentEngine(), "claude");
      assert.equal(peekAgentEngine(), "claude");
      // A save during a refresh wins over the refresh's older answer.
      let answer: (res: Response) => void = () => {};
      globalThis.fetch = (async (url: string, init?: RequestInit) => {
        if (init?.method === "PUT") {
          return new Response(JSON.stringify({ engine: "codex" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (String(url).endsWith("/account/agent-engine")) {
          return new Promise<Response>((resolve) => (answer = resolve));
        }
        return new Response(JSON.stringify(status([cli("claude"), cli("codex")])), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }) as typeof fetch;
      await loadLocalStatus(true);
      const refreshing = loadAgentEngine();
      assert.equal(await saveAgentEngine("codex"), "codex");
      answer(
        new Response(JSON.stringify({ engine: "claude" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
      assert.equal(await refreshing, "codex");
      assert.equal(peekAgentEngine(), "codex");
    } finally {
      globalThis.fetch = realFetch;
      if (realLocal === undefined) delete process.env.APP_LOCAL_MODE;
      else process.env.APP_LOCAL_MODE = realLocal;
    }
  });

  void it("only an engine the backend says is stored counts as saved", async () => {
    let body: Record<string, unknown> = { engine: "claude" };
    const realFetch = globalThis.fetch;
    const realLocal = process.env.APP_LOCAL_MODE;
    process.env.APP_LOCAL_MODE = "1";
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      const res = !String(url).endsWith("/account/agent-engine")
        ? status([cli("claude"), cli("codex")])
        : init?.method === "PUT"
          ? { engine: "claude" }
          : body;
      return new Response(JSON.stringify(res), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;
    try {
      // An older backend sends no `stored` field: the engine is the automatic pick.
      await loadLocalStatus(true);
      assert.equal(await loadAgentEngine(), "claude");
      assert.equal(peekAgentEngineStored(), false);
      body = { engine: "claude", stored: false };
      await loadLocalStatus(true);
      await loadAgentEngine();
      assert.equal(peekAgentEngineStored(), false);
      assert.equal(await saveAgentEngine("claude"), "claude");
      assert.equal(peekAgentEngineStored(), true);
      body = { engine: "claude", stored: true };
      await loadLocalStatus(true);
      await loadAgentEngine();
      assert.equal(peekAgentEngineStored(), true);
    } finally {
      globalThis.fetch = realFetch;
      if (realLocal === undefined) delete process.env.APP_LOCAL_MODE;
      else process.env.APP_LOCAL_MODE = realLocal;
    }
  });

  void it("a saved Droid engine falls back to the default CLI", async () => {
    const realFetch = globalThis.fetch;
    const realLocal = process.env.APP_LOCAL_MODE;
    process.env.APP_LOCAL_MODE = "1";
    globalThis.fetch = (async (url: string) => {
      const body = String(url).endsWith("/account/agent-engine")
        ? { engine: "droid" }
        : status([cli("claude", { logged_in: false }), cli("codex")]);
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;
    try {
      await loadLocalStatus(true);
      assert.equal(await loadAgentEngine(), null);
      assert.equal(peekAgentEngine(), null);
      // With no engine, every role runs on the first ready CLI.
      assert.match((await localAgentModel(null)) ?? "", /^subscription\/codex\//);
    } finally {
      globalThis.fetch = realFetch;
      if (realLocal === undefined) delete process.env.APP_LOCAL_MODE;
      else process.env.APP_LOCAL_MODE = realLocal;
    }
  });

  void it("a failed reload keeps the stored engine, so agent requests keep its CLI", async () => {
    let engineFails = false;
    const realFetch = globalThis.fetch;
    const realNow = Date.now;
    const realLocal = process.env.APP_LOCAL_MODE;
    process.env.APP_LOCAL_MODE = "1";
    globalThis.fetch = (async (url: string) => {
      if (String(url).endsWith("/account/agent-engine")) {
        return new Response(
          JSON.stringify(engineFails ? { detail: "boom" } : { engine: "codex" }),
          {
            status: engineFails ? 500 : 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      }
      const s = status([
        cli("claude", { models: ["claude-sonnet"] }),
        cli("codex", { models: ["openai/gpt-5"] }),
      ]);
      return new Response(JSON.stringify(s), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;
    try {
      await loadLocalStatus(true);
      assert.equal(await loadAgentEngine(), "codex");
      assert.match((await localAgentModel(null)) ?? "", /^subscription\/codex\//);
      // The status cache expires, so the next request refreshes it and reloads the engine.
      engineFails = true;
      Date.now = () => realNow() + 60_000;
      assert.match((await localAgentModel(null)) ?? "", /^subscription\/codex\//);
      assert.equal(peekAgentEngine(), "codex");
    } finally {
      globalThis.fetch = realFetch;
      Date.now = realNow;
      if (realLocal === undefined) delete process.env.APP_LOCAL_MODE;
      else process.env.APP_LOCAL_MODE = realLocal;
    }
  });
});

void describe("OpenRouter engine (SKY-123 AC3)", () => {
  const s = status([cli("codex"), cli("claude")]);

  void it("with a verified key, OpenRouter is a choice with no reason", () => {
    setClientLocale("en");
    const option = agentEngineOptions(s, true, new Date(), true).find(
      (o) => o.engine === "openrouter",
    )!;
    assert.equal(option.disabled, false);
    const html = renderToStaticMarkup(<EngineChoice option={option} status={s} failed={false} />);
    assert.match(html, /settings\.agent_engine\.openrouter</);
    assert.doesNotMatch(html, /settings\.agent_engine\.openrouter_local/);
  });

  void it("without a verified key, OpenRouter is disabled and says why", () => {
    setClientLocale("en");
    const option = agentEngineOptions(s, true, new Date(), false).find(
      (o) => o.engine === "openrouter",
    )!;
    assert.equal(option.disabled, true);
    const html = renderToStaticMarkup(<EngineChoice option={option} status={s} failed={false} />);
    assert.match(html, /settings\.agent_engine\.openrouter_local/);
  });

  void it("while the key list loads or failed, OpenRouter says so, not that the key is missing (SKY-134)", () => {
    setClientLocale("en");
    const option = agentEngineOptions(s, true, new Date(), null).find(
      (o) => o.engine === "openrouter",
    )!;
    assert.equal(option.disabled, true);
    const loading = renderToStaticMarkup(
      <EngineChoice option={option} status={s} failed={false} />,
    );
    assert.match(loading, /settings\.agent_engine\.openrouter_keys_loading</);
    assert.doesNotMatch(loading, /openrouter_local|openrouter_keys_failed/);
    const failed = renderToStaticMarkup(
      <EngineChoice option={option} status={s} failed={false} keysFailed />,
    );
    assert.match(failed, /settings\.agent_engine\.openrouter_keys_failed</);
    assert.doesNotMatch(failed, /openrouter_local|openrouter_keys_loading/);
  });

  void it("the selected OpenRouter engine shows its name and its own description", () => {
    const ok = render("openrouter", s, true);
    assert.match(ok, /data-engine-logo="openrouter"/);
    assert.match(ok, /settings\.agent_engine\.description_openrouter</);
    assert.doesNotMatch(ok, /settings\.agent_engine\.description_local/);
    assert.doesNotMatch(ok, /role="status"/);
    // While the key list loads, the engine stays OpenRouter with no warning.
    const loading = render("openrouter", s, null);
    assert.match(loading, /data-engine-logo="openrouter"/);
    assert.doesNotMatch(loading, /role="status"/);
  });

  void it("a stored OpenRouter engine with the key gone shows the CLI agents run on, and why", () => {
    const gone = render("openrouter", s, false);
    // The trigger names the default CLI, not OpenRouter.
    assert.match(gone, /data-engine-logo="codex"/);
    assert.doesNotMatch(gone, /data-engine-logo="openrouter"/);
    assert.match(gone, /settings\.agent_engine\.description_local</);
    assert.match(gone, /role="status"/);
    assert.match(gone, /settings\.agent_engine\.openrouter_fallback/);
    // A ready fallback CLI needs no reason.
    assert.doesNotMatch(gone, /subscription\.state\./);
  });

  void it("with the key gone and the fallback CLI signed out, the warning keeps its sign-in command", () => {
    const html = render("openrouter", status([cli("claude", { logged_in: false })]), false);
    assert.match(html, /data-engine-logo="claude"/);
    assert.match(html, /settings\.agent_engine\.openrouter_fallback/);
    const warning = html.slice(html.indexOf('role="status"'));
    assert.match(warning, /subscription\.state\.logged_out/);
    assert.match(warning, /<code[^>]*>claude auth login<\/code>/);
  });

  void it("with the key gone, choosing the shown CLI stores it", () => {
    setClientLocale("en");
    const saved: AgentEngine[] = [];
    const tree = AgentEngineSelect({
      status: s,
      value: "openrouter",
      keyVerified: false,
      saving: false,
      onChange: (engine) => saved.push(engine),
    });
    const find = (node: React.ReactNode): React.ReactElement | null => {
      if (!React.isValidElement(node)) return null;
      if (node.type === Select) return node;
      const { children } = node.props as { children?: React.ReactNode };
      for (const child of React.Children.toArray(children)) {
        const hit = find(child);
        if (hit) return hit;
      }
      return null;
    };
    const select = find(tree)!.props as {
      value: string;
      onValueChange: (next: string) => void;
    };
    // Radix fires onValueChange only for a value other than the one it holds.
    assert.notEqual(select.value, "codex");
    select.onValueChange("codex");
    assert.deepEqual(saved, ["codex"]);
  });
});
