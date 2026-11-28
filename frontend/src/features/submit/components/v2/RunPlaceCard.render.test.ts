import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { it } from "node:test";

const frontend = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../../../..");

// node --test only loads .ts, so the .tsx render test runs in a jiti child.
void it("RunPlaceCard.client.test.tsx passes", () => {
  // The card reads the wizard through `../../hooks/use-unified-wizard`, whose
  // provider needs a router, a session and a budget. The child aliases that
  // import to this stub, which calls the shell hook the test puts on globalThis.
  const stubDir = mkdtempSync(path.join(tmpdir(), "run-place-card-"));
  const stub = path.join(stubDir, "wizard-stub.ts");
  try {
    writeFileSync(
      stub,
      "export const useWizardV2ShellOptional = () => (globalThis as { __runPlaceShell?: () => unknown }).__runPlaceShell?.() ?? null;\n",
    );
    // Without NODE_TEST_CONTEXT the child prints its own report, so the
    // `fail 0` check below can read it.
    const { NODE_TEST_CONTEXT: _parent, ...env } = process.env;
    const result = spawnSync(
      path.join(frontend, "node_modules/.bin/jiti"),
      ["src/features/submit/components/v2/RunPlaceCard.client.test.tsx"],
      {
        cwd: frontend,
        encoding: "utf8",
        timeout: 60_000,
        env: {
          ...env,
          JITI_JSX: "1",
          JITI_ALIAS: JSON.stringify({
            "@/": `${path.join(frontend, "src")}/`,
            "../../hooks/use-unified-wizard": stub,
          }),
        },
      },
    );
    assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
    // Node 22 prints TAP (`#`) to a pipe; Node 24 prints its spec format (`ℹ`).
    assert.match(result.stdout, /[#ℹ] pass 1\n[#ℹ] fail 0/);
  } finally {
    rmSync(stubDir, { recursive: true, force: true });
  }
});
