import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { it } from "node:test";

const frontend = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../../..");
// The dialog's imports pull in a stylesheet that node cannot load.
const stub = path.join(
  frontend,
  "src/features/optimizations/components/__fixtures__/style-stub.ts",
);

// node --test only loads .ts, so the .tsx render test runs in a jiti child.
void it("ModelConfigModal.client.test.tsx passes", () => {
  const result = spawnSync(
    path.join(frontend, "node_modules/.bin/jiti"),
    ["src/features/submit/components/ModelConfigModal.client.test.tsx"],
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
