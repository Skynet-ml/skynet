import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { it } from "node:test";
import { fileURLToPath } from "node:url";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
const stub = path.join(frontend, "src/features/optimizations/components/__fixtures__/style-stub.ts");

// node --test only loads .ts, so the .tsx DayRow test runs in a jiti child.
void it("Usage DayRow client tests pass", () => {
  const result = spawnSync(
    path.join(frontend, "node_modules/.bin/jiti"),
    ["src/features/billing/components/usage-day-row.client.test.tsx"],
    {
      cwd: frontend,
      encoding: "utf8",
      env: {
        ...process.env,
        NODE_TEST_CONTEXT: undefined,
        JITI_JSX: "1",
        JITI_ALIAS: JSON.stringify({
          "@/": `${path.join(frontend, "src")}/`,
          "react-loading-skeleton/dist/skeleton.css": stub,
        }),
      },
    },
  );
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});
