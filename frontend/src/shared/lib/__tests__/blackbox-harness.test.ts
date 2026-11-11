import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { test } from "node:test";

const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = (key) => `msg:${key}`;",
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

const { BLACKBOX_HARNESSES, effortHarness, harnessEfforts, harnessLabel, keepHarnessEffort } =
  await import("../blackbox-harness.ts");

const REPO = new URL("../../../../../", import.meta.url);
const read = (path: string) => readFileSync(new URL(path, REPO), "utf8");
const HARNESS_DIR = "backend/core/service_gateway/optimization/blackbox/harnesses/";

// The quoted words of the tuple or dict a module assigns to `name`; dict keys only.
function backendLevels(source: string, name: string): string[] | null {
  const start = source.search(new RegExp(`^${name} = [({]`, "m"));
  if (start < 0) return null;
  const rest = source.slice(start);
  const block = rest.slice(0, rest.search(/[)}]/) + 1);
  const keys = [...block.matchAll(/"(\w+)":/g)].map((m) => m[1]!);
  return keys.length ? keys : [...block.matchAll(/"(\w+)"/g)].map((m) => m[1]!);
}

test("the picker offers every harness the backend accepts, in its order, minus custom", () => {
  const models = read("backend/core/models/blackbox.py");
  const ids = Object.fromEntries(
    [...models.matchAll(/^(BLACKBOX_HARNESS_\w+) = "(\w+)"$/gm)].map((m) => [m[1], m[2]]),
  );
  const tuple = models.slice(models.indexOf("BLACKBOX_HARNESSES = ("));
  const order = [...tuple.slice(0, tuple.indexOf(")")).matchAll(/(BLACKBOX_HARNESS_\w+)/g)].map(
    (m) => ids[m[1]!],
  );
  assert.deepEqual(
    BLACKBOX_HARNESSES,
    order.filter((id) => id !== "custom"),
  );
});

test("every harness has a label in the base and English catalogs", () => {
  for (const locale of ["he", "en"]) {
    const catalog = JSON.parse(read(`i18n/locales/ui/${locale}.json`));
    for (const harness of [...BLACKBOX_HARNESSES, "custom"]) {
      const label = catalog[`submit.blackbox.start.harness.${harness}`];
      assert.ok(typeof label === "string" && label.trim(), `${locale}: ${harness}`);
    }
  }
});

test("a stored run's retired harness shows its raw id; a known one shows its label", () => {
  assert.equal(harnessLabel("claude_code"), "claude_code");
  assert.equal(harnessLabel("hermes"), "msg:submit.blackbox.start.harness.hermes");
  assert.equal(harnessLabel("custom"), "msg:submit.blackbox.start.harness.custom");
});

test("each harness offers exactly the effort levels its backend module declares", () => {
  const pi = read(`${HARNESS_DIR}pi.py`);
  for (const harness of BLACKBOX_HARNESSES) {
    const source = read(`${HARNESS_DIR}${harness}.py`);
    const declared = source.match(/supported_efforts=(?:tuple\()?(\w+)/)?.[1];
    const expected = declared
      ? (backendLevels(source, declared) ?? backendLevels(pi, declared))
      : null;
    if (declared) assert.notEqual(expected, null, `${harness}: cannot read ${declared}`);
    assert.deepEqual(harnessEfforts(harness), expected, harness);
  }
});

test("a harness change drops only an effort the new harness refuses", () => {
  const max = { name: "m", extra: { reasoning_effort: "max" } };
  assert.deepEqual(keepHarnessEffort(max, "opencode_v2"), { name: "m", extra: undefined });
  assert.equal(keepHarnessEffort(max, "codex"), max);
  // OpenCode declares no levels, so the backend takes every one.
  assert.equal(keepHarnessEffort(max, "opencode"), max);
  const withKey = { name: "m", extra: { reasoning_effort: "none", economy_mode: true } };
  assert.deepEqual(keepHarnessEffort(withKey, "antigravity_sdk"), {
    name: "m",
    extra: { economy_mode: true },
  });
});

test("only a native engine's harness without a host CLI limits the effort", () => {
  const v2 = { harness: "opencode_v2" as const };
  assert.equal(effortHarness(v2, true), "opencode_v2");
  assert.equal(effortHarness({ ...v2, host_cli: "codex" }, true), null);
  assert.equal(effortHarness({ ...v2, host_cli: null }, true), "opencode_v2");
  assert.equal(effortHarness(v2, false), null);
});

test("leaving a host CLI or returning to a native engine trims a refused effort", () => {
  const max = { name: "m", extra: { reasoning_effort: "max" } };
  const fit = (
    proposer: { harness: "opencode_v2"; host_cli?: "codex" | null },
    native: boolean,
  ) => {
    const harness = effortHarness(proposer, native);
    return harness ? keepHarnessEffort(max, harness) : max;
  };
  assert.equal(fit({ harness: "opencode_v2", host_cli: "codex" }, true), max);
  assert.deepEqual(fit({ harness: "opencode_v2", host_cli: null }, true), {
    name: "m",
    extra: undefined,
  });
  assert.equal(fit({ harness: "opencode_v2" }, false), max);
  assert.deepEqual(fit({ harness: "opencode_v2" }, true), { name: "m", extra: undefined });
});
