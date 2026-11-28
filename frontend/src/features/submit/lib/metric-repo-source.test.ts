import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const srcUrl = new URL("../../../", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    }
    if (specifier.startsWith(".") && !specifier.endsWith(".ts")) {
      return { shortCircuit: true, url: new URL(`${specifier}.ts`, context.parentURL).href };
    }
    return nextResolve(specifier, context);
  },
});

const { emptyRepoDraft } = await import("./wizard-v2.ts");
const { metricRepoSource } = await import("./metric-repo-source.ts");

const hosted = {
  ...emptyRepoDraft(),
  provider: "gitlab" as const,
  repository: "acme/app",
  branch: "main",
  commit: "",
  editablePaths: ["src/app.py"],
};

void test("a local draft that still has a repository name reads the folder, not the host", () => {
  assert.deepEqual(metricRepoSource({ ...hosted, localPath: " /Users/me/app/ " }), {
    kind: "local",
    folder: "/Users/me/app/",
  });
});

void test("a local draft with no folder reads nothing", () => {
  assert.equal(metricRepoSource({ ...hosted, localPath: "  " }), null);
});

void test("a hosted draft reads its host at the commit, else the branch", () => {
  assert.deepEqual(metricRepoSource(hosted), {
    kind: "host",
    repository: "acme/app",
    ref: "main",
    host: "gitlab",
  });
  const pinned = { ...hosted, provider: undefined, commit: " abc1234 " };
  assert.deepEqual(metricRepoSource(pinned), {
    kind: "host",
    repository: "acme/app",
    ref: "abc1234",
    host: "github",
  });
});

void test("no draft or no repository name reads nothing", () => {
  assert.equal(metricRepoSource(null), null);
  assert.equal(metricRepoSource({ ...hosted, repository: " " }), null);
});
