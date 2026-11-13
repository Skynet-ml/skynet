import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { it } from "node:test";
import { fileURLToPath } from "node:url";

// fileURLToPath decodes %20 and the like, so a path with spaces still resolves.
const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");

// node --test only loads .ts, so the .tsx render test runs in a jiti child.
void it("TurnActivity render tests pass", () => {
  const result = spawnSync(
    path.join(frontend, "node_modules/.bin/jiti"),
    ["src/shared/ui/agent/turn-activity.test.tsx"],
    {
      cwd: frontend,
      encoding: "utf8",
      // A jsdom child can hang on a failed assert; a timeout makes it fail instead.
      timeout: 120_000,
      env: {
        ...process.env,
        JITI_JSX: "1",
        JITI_ALIAS: JSON.stringify({ "@/": `${path.join(frontend, "src")}/` }),
      },
    },
  );
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});
