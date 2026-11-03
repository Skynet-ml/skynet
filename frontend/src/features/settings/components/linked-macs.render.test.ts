import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { it } from "node:test";

const frontend = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../../..");

// node --test only loads .ts, so the .tsx render test runs in a jiti child.
void it("Linked Macs render tests pass", () => {
  const result = spawnSync(
    path.join(frontend, "node_modules/.bin/jiti"),
    ["src/features/settings/components/linked-macs.test.tsx"],
    {
      cwd: frontend,
      encoding: "utf8",
      env: {
        ...process.env,
        JITI_JSX: "1",
        JITI_ALIAS: JSON.stringify({ "@/": `${path.join(frontend, "src")}/` }),
      },
    },
  );
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});
