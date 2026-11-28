import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { it } from "node:test";
import { fileURLToPath } from "node:url";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

// node --test only loads .ts, so the .tsx client test runs in a jiti child.
void it("ShellNavControls client tests pass", () => {
  const result = spawnSync(
    path.join(frontend, "node_modules/.bin/jiti"),
    ["src/shared/layout/shell-nav-controls.client.test.tsx"],
    {
      cwd: frontend,
      encoding: "utf8",
      timeout: 60_000,
      env: {
        ...process.env,
        NODE_TEST_CONTEXT: undefined,
        JITI_JSX: "1",
        JITI_ALIAS: JSON.stringify({
          "next/navigation": path.join(frontend, "src/shared/layout/__fixtures__/next-navigation-mock.ts"),
          "@/": `${path.join(frontend, "src")}/`,
        }),
      },
    },
  );
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});
