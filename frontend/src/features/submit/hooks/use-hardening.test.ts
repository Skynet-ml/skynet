/*
 * use-hardening.ts imports the wizard context (.tsx) and `@/` paths, which
 * node --test cannot load. Under `npm run test:unit` this file only spawns
 * itself in a jiti child, where the cases below run.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const self = fileURLToPath(import.meta.url);
const frontend = path.resolve(path.dirname(self), "../../../..");

if (!process.env.JITI_ALIAS) {
  void it("use-hardening tests pass under jiti", () => {
    // Without NODE_TEST_CONTEXT the child prints its own TAP report, not the
    // parent's binary test stream, so the `# fail 0` check below can read it.
    const { NODE_TEST_CONTEXT: _parent, ...env } = process.env;
    const result = spawnSync(path.join(frontend, "node_modules/.bin/jiti"), [self], {
      cwd: frontend,
      encoding: "utf8",
      env: {
        ...env,
        JITI_JSX: "1",
        JITI_ALIAS: JSON.stringify({ "@/": `${path.join(frontend, "src")}/` }),
      },
    });
    assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
    assert.match(result.stdout, /# fail 0/);
  });
} else {
  const React = await import("react");
  // The classic JSX transform the test loader applies reads a global React.
  (globalThis as { React?: unknown }).React = React;
  const { registerHooks } = await import("node:module");
  // The wizard context pulls in a stylesheet, which Node cannot load.
  registerHooks({
    load: (url, context, next) =>
      url.endsWith(".css") ? { format: "module", source: "", shortCircuit: true } : next(url, context),
  });
  const { hardeningTarget } = await import("./use-hardening");
  const { emptyRepoDraft } = await import("../lib/wizard-v2");

  void describe("hardeningTarget", () => {
    void it("sends a valid repository draft as the run submit's repo target, trimmed and without secrets", () => {
      const target = hardeningTarget({
        ...emptyRepoDraft(),
        repository: "  acme/widgets ",
        branch: " main ",
        commit: "",
        editablePaths: [" src/ ", "", "  "],
        setupCommand: " pip install -e . ",
      });
      assert.deepEqual(target, {
        kind: "repo",
        repo: {
          provider: "github",
          repository: "acme/widgets",
          branch: "main",
          commit: null,
          editable_paths: ["src/"],
        },
        setup_command: "pip install -e .",
      });
      assert.equal(target?.repo?.secrets, undefined);
    });

    void it("marks a repository Skynet created", () => {
      const target = hardeningTarget({ ...emptyRepoDraft(), repository: "acme/new", createdBySkynet: true });
      assert.equal(target?.repo?.created_by_skynet, true);
      assert.equal(target?.setup_command, null);
    });

    void it("sends no target for a setup without a repository", () => {
      assert.equal(hardeningTarget(null), null);
      assert.equal(hardeningTarget(undefined), null);
    });

    void it("refuses a broken repository draft with its issue key, so no job starts", () => {
      assert.throws(
        () => hardeningTarget({ ...emptyRepoDraft(), repository: "not a repo" }),
        { message: "submit.v2.repo.issue.repository" },
      );
      assert.throws(
        () => hardeningTarget({ ...emptyRepoDraft(), repository: "acme/widgets", editablePaths: [" "] }),
        { message: "submit.v2.repo.issue.paths" },
      );
    });
  });
}
