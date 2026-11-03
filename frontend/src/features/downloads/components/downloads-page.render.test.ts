import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { it } from "node:test";

const frontend = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../../..");

for (const suite of ["downloads-page.test.tsx"]) {
  void it(`Downloads ${suite} passes`, () => {
    const result = spawnSync(
      path.join(frontend, "node_modules/.bin/jiti"),
      [`src/features/downloads/components/${suite}`],
      {
        cwd: frontend,
        encoding: "utf8",
        env: {
          ...process.env,
          NODE_TEST_CONTEXT: undefined,
          JITI_JSX: "1",
          JITI_ALIAS: JSON.stringify({ "@/": `${path.join(frontend, "src")}/` }),
        },
      },
    );
    assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  });
}
