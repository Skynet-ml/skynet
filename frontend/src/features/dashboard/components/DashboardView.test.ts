import assert from "node:assert/strict";
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
