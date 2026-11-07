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
  const { hardeningErrorText, hardeningTarget, requestHardeningJob } = await import("./use-hardening");
  const { emptyRepoDraft } = await import("../lib/wizard-v2");
  const { msg } = await import("@/shared/lib/messages");
  const { buildActiveCatalog } = await import("@/shared/lib/messages.server");
  const { setClientLocale } = await import("@/shared/lib/runtime-locale");
  const { setServerMessages } = await import("@/shared/lib/runtime-messages");
  setServerMessages(buildActiveCatalog("en"), "en");
  setClientLocale("en");

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

  const settings = {
    rounds: 3,
    round_cap_usd: 2,
    attacker_model: "attacker",
    judge_model: "judge",
    view: "black_box" as const,
  };

  void describe("requestHardeningJob", () => {
    void it("sends the repository target and the wizard budget id with the settings", async () => {
      const sent: unknown[] = [];
      const job = { id: "job-1" };
      const result = await requestHardeningJob(
        settings,
        "draft-1",
        { ...emptyRepoDraft(), repository: "acme/widgets" },
        async () => ({ id: "budget-1" }),
        async (body) => {
          sent.push(body);
          return job as never;
        },
      );
      assert.equal(result, job);
      assert.deepEqual(sent, [
        {
          ...settings,
          draft_id: "draft-1",
          target: {
            kind: "repo",
            repo: {
              provider: "github",
              repository: "acme/widgets",
              branch: null,
              commit: null,
              editable_paths: ["."],
            },
            setup_command: null,
          },
          binding_id: "budget-1",
        },
      ]);
    });

    void it("sends a null target without a repository", async () => {
      let body: { target?: unknown; binding_id?: unknown } | null = null;
      await requestHardeningJob(settings, "draft-1", null, async () => ({ id: "budget-2" }), async (b) => {
        body = b;
        return {} as never;
      });
      assert.equal(body!.target, null);
      assert.equal(body!.binding_id, "budget-2");
    });

    void it("opens no budget and sends no request for a broken repository", async () => {
      let budgets = 0;
      let creates = 0;
      await assert.rejects(
        requestHardeningJob(
          settings,
          "draft-1",
          { ...emptyRepoDraft(), repository: "not a repo" },
          async () => {
            budgets += 1;
            return { id: "budget-1" };
          },
          async () => {
            creates += 1;
            return {} as never;
          },
        ),
        { message: "submit.v2.repo.issue.repository" },
      );
      assert.equal(budgets, 0);
      assert.equal(creates, 0);
    });
  });

  void describe("hardeningErrorText", () => {
    void it("translates the budget and repository issue keys", () => {
      assert.equal(hardeningErrorText(new Error("budget.invalid")), msg("budget.invalid"));
      assert.notEqual(msg("budget.invalid"), "budget.invalid");
      assert.equal(
        hardeningErrorText(new Error("submit.v2.repo.issue.repository")),
        msg("submit.v2.repo.issue.repository"),
      );
      assert.notEqual(msg("submit.v2.repo.issue.repository"), "submit.v2.repo.issue.repository");
    });

    void it("keeps a server message as it is", () => {
      assert.equal(hardeningErrorText(new Error("A hardening job is already running.")), "A hardening job is already running.");
    });

    void it("falls back to the generic message for an empty error", () => {
      assert.equal(hardeningErrorText(new Error("")), msg("errors.generic"));
      assert.notEqual(msg("errors.generic"), "errors.generic");
      assert.equal(hardeningErrorText(undefined), msg("errors.generic"));
    });

    void it("shows no toast for an aborted request", () => {
      const abort = new Error("The operation was aborted.");
      abort.name = "AbortError";
      assert.equal(hardeningErrorText(abort), null);
      assert.equal(hardeningErrorText(new DOMException("aborted", "AbortError")), null);
    });
  });
}
