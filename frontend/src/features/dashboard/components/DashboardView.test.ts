import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import path from "node:path";
import { it } from "node:test";
import * as React from "react";
import { createJiti } from "jiti";

// The component graph pulls in stylesheets that Node cannot load.
registerHooks({
  load: (url, context, next) =>
    url.endsWith(".css")
      ? { format: "module", source: "", shortCircuit: true }
      : next(url, context),
});
// Modules that render JSX at load time read the global React.
(globalThis as { React?: typeof React }).React = React;
const jiti = createJiti(import.meta.url, {
  jsx: true,
  alias: { "@/": `${path.join(process.cwd(), "src")}/` },
});

const { stoppableJobIds } = await jiti.import<{
  stoppableJobIds: (jobs: { optimization_id: string; status: string }[]) => string[];
}>("./DashboardView");

void it("bulk Stop cancels active and paused runs only (SKY-827)", () => {
  const jobs = ["running", "pending", "paused", "cancelled", "success", "failed"].map((status) => ({
    optimization_id: status,
    status,
  }));
  assert.deepEqual(stoppableJobIds(jobs), ["running", "pending", "paused"]);
});

void it("bulk Stop and its button use stoppableJobIds (SKY-827)", () => {
  // DashboardView needs the router, session and query context, so check the call sites.
  const source = readFileSync(new URL("./DashboardView.tsx", import.meta.url), "utf8").replace(
    /\s+/g,
    " ",
  );
  const ids = [...source.matchAll(/const stoppableSelectedIds = ([^;]+);/g)].map((m) => m[1]);
  assert.deepEqual(ids, ["canEditSelection ? stoppableJobIds(selectedJobs) : []"]);
  assert.match(source, /bulkCancelJobs\(stoppableSelectedIds\)/);
  assert.match(source, /if \(stoppableSelectedIds\.length === 0 \|\| selectionAction\) return;/);
  assert.match(source, /canStop=\{stoppableSelectedIds\.length > 0\}/);
});
