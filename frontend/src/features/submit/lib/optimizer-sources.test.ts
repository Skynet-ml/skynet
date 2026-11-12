import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const srcUrl = new URL("../../../", import.meta.url);

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/"))
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    if (specifier.startsWith(".") && !/\.[cm]?[jt]sx?$/.test(specifier) && context.parentURL)
      return { shortCircuit: true, url: new URL(`${specifier}.ts`, context.parentURL).href };
    return nextResolve(specifier, context);
  },
});

const { OPTIMIZER_SOURCES, optimizerSources } = await import("./optimizer-sources.ts");
const { BLACKBOX_OPTIMIZERS } = await import("@/shared/types/unified-run");

const ARXIV_ABS = /^https:\/\/arxiv\.org\/abs\/\d{4}\.\d{4,5}$/;
const GITHUB_REPO = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+$/;

void test("every paper is an arXiv abstract page and every repo a GitHub repo root", () => {
  for (const [id, sources] of Object.entries(OPTIMIZER_SOURCES)) {
    assert.ok(sources && sources.length > 0, `${id} has an empty source list`);
    for (const source of sources) {
      assert.ok(source.title.trim(), `${id} has a source without a title`);
      assert.match(source.url, source.kind === "paper" ? ARXIV_ABS : GITHUB_REPO, `${id}: ${source.url}`);
    }
  }
});

void test("only real optimizer choices carry sources", () => {
  const known = new Set<string>(BLACKBOX_OPTIMIZERS);
  for (const id of Object.keys(OPTIMIZER_SOURCES)) assert.ok(known.has(id), `unknown optimizer ${id}`);
});

void test("each optimizer links its verified primary source", () => {
  const urls = (id: Parameters<typeof optimizerSources>[0]) => optimizerSources(id).map((s) => s.url);
  assert.deepEqual(urls("gepa"), ["https://arxiv.org/abs/2507.19457"]);
  assert.deepEqual(urls("meta_harness"), ["https://arxiv.org/abs/2603.28052"]);
  assert.deepEqual(urls("autosaddler"), ["https://arxiv.org/abs/2608.23041"]);
  assert.deepEqual(urls("shinka_evolve"), ["https://arxiv.org/abs/2509.19349"]);
  assert.deepEqual(urls("autoresearch"), ["https://github.com/karpathy/autoresearch"]);
});

void test("choices without a primary source get no link", () => {
  assert.deepEqual(optimizerSources("auto"), []);
  assert.deepEqual(optimizerSources("best_of_n"), []);
});
