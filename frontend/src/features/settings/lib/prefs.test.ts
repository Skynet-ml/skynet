import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_PREFS,
  PREF_KEYS,
  parseAgentPreferencePatch,
  readPref,
} from "./prefs.ts";

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
