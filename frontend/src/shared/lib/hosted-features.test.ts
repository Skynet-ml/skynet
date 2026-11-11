import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { test } from "node:test";

import {
  createLinkStore,
  hostedFeaturesAvailable,
  LINKED_ONLY_SETTINGS_TABS,
  WEB_ONLY_SETTINGS_TABS,
  withoutHostedOnlyTabs,
} from "./hosted-features.ts";

const TABS = ["wizard", "account", "security", "privacy", "billing", "usage"] as const;

test("the web always shows hosted features and every tab", () => {
  for (const linked of [undefined, false, true]) {
    assert.equal(hostedFeaturesAvailable(false, linked), true);
  }
  assert.deepEqual(withoutHostedOnlyTabs(TABS, false, true), [...TABS]);
});

test("an unlinked Mac app hides them, also while the link is unknown", () => {
  assert.equal(hostedFeaturesAvailable(true, false), false);
  assert.equal(hostedFeaturesAvailable(true, undefined), false);
  assert.deepEqual(withoutHostedOnlyTabs(TABS, true, false), ["wizard", "account", "privacy", "usage"]);
});

test("a linked Mac app shows billing but never security", () => {
  assert.equal(hostedFeaturesAvailable(true, true), true);
  assert.deepEqual(withoutHostedOnlyTabs(TABS, true, hostedFeaturesAvailable(true, true)), [
    "wizard",
    "account",
    "privacy",
    "billing",
    "usage",
  ]);
});

test("the agent's settings tools hide the same tabs", () => {
  const router = fs.readFileSync(
    path.join(process.cwd(), "..", "backend", "core", "api", "routers", "agent_settings.py"),
    "utf8",
  );
  const backendSet = (name: string): string[] => {
    const match = router.match(new RegExp(`${name} = frozenset\\(\\{([^}]*)\\}\\)`));
    assert.ok(match, `agent_settings.py defines ${name}`);
    return [...match[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]).sort();
  };
  assert.deepEqual(backendSet("_WEB_ONLY_TABS"), [...WEB_ONLY_SETTINGS_TABS].sort());
  assert.deepEqual(backendSet("_LINKED_ONLY_TABS"), [...LINKED_ONLY_SETTINGS_TABS].sort());
});

test("the link store reads once for many listeners and tells them about changes", async () => {
  let reads = 0;
  let linked = false;
  let fire: () => void = () => {};
  let watching = false;
  const store = createLinkStore(
    async () => {
      reads += 1;
      return linked;
    },
    () => false,
    (refresh) => {
      watching = true;
      fire = refresh;
      return () => {
        watching = false;
      };
    },
  );
  assert.equal(store.getSnapshot(), undefined);

  let calls = 0;
  const offA = store.subscribe(() => (calls += 1));
  const offB = store.subscribe(() => (calls += 1));
  await store.refresh();
  assert.equal(reads, 1);
  assert.equal(store.getSnapshot(), false);
  assert.equal(watching, true);

  linked = true;
  fire();
  await store.refresh();
  assert.equal(store.getSnapshot(), true);
  assert.equal(calls, 4);

  offA();
  offB();
  assert.equal(watching, false);
});

test("a last-known link shows hosted features before the first read", () => {
  const store = createLinkStore(() => new Promise<boolean>(() => {}), () => true);
  const off = store.subscribe(() => {});
  assert.equal(store.getSnapshot(), true);
  off();
});

test("a failed read keeps the last state", async () => {
  let fail = false;
  const store = createLinkStore(
    async () => {
      if (fail) throw new Error("offline");
      return true;
    },
    () => false,
  );
  const off = store.subscribe(() => {});
  await store.refresh();
  fail = true;
  await store.refresh();
  assert.equal(store.getSnapshot(), true);
  off();
});
