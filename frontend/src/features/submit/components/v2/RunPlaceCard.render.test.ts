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
  writeFileSync(
    stub,
    "export const useWizardV2ShellOptional = () => (globalThis as { __runPlaceShell?: () => unknown }).__runPlaceShell?.() ?? null;\n",
  );
  // Without NODE_TEST_CONTEXT the child prints its own TAP report, so the
  // `# fail 0` check below can read it.
  const { NODE_TEST_CONTEXT: _parent, ...env } = process.env;
  const result = spawnSync(
    path.join(frontend, "node_modules/.bin/jiti"),
    ["src/features/submit/components/v2/RunPlaceCard.client.test.tsx"],
    {
      cwd: frontend,
      encoding: "utf8",
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
  rmSync(stubDir, { recursive: true, force: true });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  assert.match(result.stdout, /# pass 1\n# fail 0/);
});
