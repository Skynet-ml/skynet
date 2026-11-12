import { test } from "node:test";
import assert from "node:assert/strict";
import { registerHooks } from "node:module";

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith(".") && !specifier.endsWith(".ts")) {
      return { shortCircuit: true, url: new URL(`${specifier}.ts`, context.parentURL).href };
    }
    return nextResolve(specifier, context);
  },
});

const { ancestors, buildRepoTree, changedFolders, hasFile, repoFileKind } = await import(
  "./repo-browser.ts"
);
const { parsePatch } = await import("./repo-patch.ts");

const ENTRIES = [
  { path: "README.md", type: "file" as const, size: 10 },
  { path: "src", type: "dir" as const, size: null },
  { path: "src/app.py", type: "file" as const, size: 20 },
  { path: "src/old.py", type: "file" as const, size: 5 },
  { path: "docs", type: "dir" as const, size: null },
  { path: "docs/guide.md", type: "file" as const, size: 5 },
];

const PATCH = [
  "diff --git a/src/app.py b/src/app.py",
  "--- a/src/app.py",
  "+++ b/src/app.py",
  "@@ -1 +1 @@",
  "-x",
  "+y",
  "diff --git a/src/old.py b/src/old.py",
  "deleted file mode 100644",
  "--- a/src/old.py",
  "+++ /dev/null",
  "@@ -1 +0,0 @@",
  "-gone",
  "diff --git a/src/new/mod.py b/src/new/mod.py",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/src/new/mod.py",
  "@@ -0,0 +1 @@",
  "+hi",
].join("\n");

const root = buildRepoTree(ENTRIES, parsePatch(PATCH));

test("buildRepoTree lists folders before files and adds the patch's new files", () => {
  assert.deepEqual(
    root.children.map((c) => c.name),
    ["docs", "src", "README.md"],
  );
  const src = root.children.find((c) => c.name === "src");
  assert.deepEqual(
    src?.children.map((c) => c.name),
    ["new", "app.py", "old.py"],
  );
  assert.equal(src?.changedCount, 3);
});

test("buildRepoTree marks deleted files as removed", () => {
  const src = root.children.find((c) => c.name === "src");
  const old = src?.children.find((c) => c.name === "old.py");
  assert.equal(old?.removed, true);
  assert.equal(old?.change?.status, "deleted");
});

test("changedFolders opens only folders holding a change", () => {
  assert.deepEqual([...changedFolders(root)].sort(), ["src", "src/new"]);
});

test("hasFile finds files, not folders", () => {
  assert.equal(hasFile(root, "src/new/mod.py"), true);
  assert.equal(hasFile(root, "src"), false);
  assert.equal(hasFile(root, "src/missing.py"), false);
});

test("ancestors lists the folders above a path", () => {
  assert.deepEqual(ancestors("a/b/c.txt"), ["a", "a/b"]);
  assert.deepEqual(ancestors("c.txt"), []);
});

test("repoFileKind reads the extension", () => {
  assert.equal(repoFileKind("docs/README.MD"), "markdown");
  assert.equal(repoFileKind("site/index.html"), "html");
  assert.equal(repoFileKind("a.py"), "python");
  assert.equal(repoFileKind("Makefile"), "code");
});

test("buildRepoTree counts files and changed files", () => {
  const root = buildRepoTree(ENTRIES, parsePatch(PATCH));
  assert.equal(root.fileCount, 5);
  assert.equal(root.changedCount, 3);
});
