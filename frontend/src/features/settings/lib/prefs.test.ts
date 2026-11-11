import { test } from "node:test";
import assert from "node:assert/strict";
import { registerHooks } from "node:module";

const unifiedRunUrl = new URL("../../../shared/types/unified-run.ts", import.meta.url).href;

// prefs.ts imports unified-run through the "@/" alias, which node cannot resolve alone.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/types/unified-run") return { shortCircuit: true, url: unifiedRunUrl };
    return nextResolve(specifier, context);
  },
});

const {
  DEFAULT_PREFS,
  PREF_KEYS,
  composerSeed,
  parseAgentPreferencePatch,
  readPref,
  rememberComposerPick,
} = await import("./prefs.ts");

test("parseAgentPreferencePatch accepts the validated agent response envelope", () => {
  assert.deepEqual(
    parseAgentPreferencePatch({
      updates: {
        wizard_split_mode: "manual",
        agent_trust_mode: "yolo",
      },
    }),
    {
      wizardSplitMode: "manual",
    },
  );
});

test("parseAgentPreferencePatch handles nested JSON and ignores invalid fields", () => {
  assert.deepEqual(
    parseAgentPreferencePatch(
      JSON.stringify({
        result: {
          updates: {
            tagger_assist: false,
            wizard_code_assist: "unknown",
            unsupported: true,
          },
        },
      }),
    ),
    { taggerAssist: false },
  );
});

test("the retired expand-advanced pref is gone: no key, no default, agent patches drop it", () => {
  assert.equal("expandAdvanced" in PREF_KEYS, false);
  assert.equal("expandAdvanced" in DEFAULT_PREFS, false);
  assert.equal(Object.values(PREF_KEYS).includes("skynet.prefs.expand-advanced"), false);
  assert.deepEqual(parseAgentPreferencePatch({ updates: { expand_advanced: true } }), {});
});

test("readPref falls back to the default outside the browser", () => {
  assert.equal(readPref("taggerAssist"), true);
});

test("new optimizations default to Auto, the catalog's models and the wizard's own budget", () => {
  assert.equal(readPref("optimizerEngine"), "auto");
  assert.deepEqual(readPref("optimizerTaskModel"), { name: "" });
  assert.deepEqual(readPref("optimizerReflectionModel"), { name: "" });
  assert.equal(readPref("optimizerMaxRuns"), null);
});

test("a saved DSPy default engine reads as Auto; a repository engine stays", () => {
  const store = new Map<string, string>();
  const g = globalThis as { window?: unknown };
  g.window = {
    localStorage: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => store.set(k, v),
    },
  };
  try {
    for (const legacy of ["dspy_gepa", "dspy_miprov2", "dspy_bootstrap_fewshot"]) {
      store.set(PREF_KEYS.optimizerEngine, JSON.stringify(legacy));
      assert.equal(readPref("optimizerEngine"), "auto");
    }
    store.set(PREF_KEYS.optimizerEngine, JSON.stringify("shinka_evolve"));
    assert.equal(readPref("optimizerEngine"), "shinka_evolve");
    store.set(PREF_KEYS.optimizerEngine, JSON.stringify("unknown_engine"));
    assert.equal(readPref("optimizerEngine"), "auto");
  } finally {
    delete g.window;
  }
});

test("only the Mac app starts a conversation on the last sent composer pick", () => {
  const store = new Map<string, string>();
  const g = globalThis as { window?: unknown };
  g.window = {
    localStorage: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => store.set(k, v),
    },
  };
  try {
    assert.deepEqual(composerSeed(true), { model: null, effort: null });
    rememberComposerPick(false, "openai/gpt-web", "high");
    assert.equal(store.size, 0);
    rememberComposerPick(true, "subscription/claude/anthropic/claude-opus-4.7", "high");
    assert.deepEqual(composerSeed(true), {
      model: "subscription/claude/anthropic/claude-opus-4.7",
      effort: "high",
    });
    assert.deepEqual(composerSeed(false), { model: null, effort: null });
  } finally {
    delete g.window;
  }
});
