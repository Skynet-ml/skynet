import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

// Only the effort labels read messages; the unpicked-effort rule doesn't.
const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = (key) => key;",
};

registerHooks({
  resolve(specifier, context, nextResolve) {
    const source = stubs[specifier];
    if (source !== undefined) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(source)}` };
    }
    return nextResolve(specifier, context);
  },
});

const { unpickedEffort, supportsEffortSlider } = await import("./model-efforts.ts");

test("an opt-in thinker runs low when no effort is picked, so Auto keeps it thinking", () => {
  assert.equal(
    unpickedEffort({
      reasoning_efforts: ["minimal", "low", "medium", "high"],
      default_reasoning_effort: "medium",
      reasoning_default_enabled: false,
    }),
    "low",
  );
});

test("a default-on thinker runs its catalog default, and a non-thinker runs none", () => {
  assert.equal(
    unpickedEffort({
      reasoning_efforts: ["low", "high"],
      default_reasoning_effort: "high",
      reasoning_default_enabled: true,
    }),
    "high",
  );
  assert.equal(unpickedEffort({ reasoning_efforts: [], reasoning_default_enabled: false }), null);
  assert.equal(unpickedEffort(undefined), null);
});


test("only models with a usable effort slider remain supported", () => {
  const model = { value: "openrouter/example/model", supports_thinking: true };
  assert.equal(supportsEffortSlider({ ...model, reasoning_efforts: ["none", "low"] }), true);
  assert.equal(supportsEffortSlider({ ...model, reasoning_efforts: [] }), false);
  assert.equal(supportsEffortSlider({ ...model, reasoning_efforts: ["none"] }), false);
  assert.equal(supportsEffortSlider({ ...model, supports_thinking: false }), false);
  assert.equal(supportsEffortSlider({ ...model, reasoning_efforts: ["unknown"] }), false);
  assert.equal(supportsEffortSlider(model), true);
});
