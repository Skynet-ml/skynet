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

const { canCancelRun } = await jiti.import<{
  canCancelRun: (canEdit: boolean, status: string) => boolean;
}>("./Sidebar");

void it("offers Cancel on an active or paused run the user can edit (SKY-827)", () => {
  const cases: [boolean, string, boolean][] = [
    [true, "running", true],
    [true, "pending", true],
    [true, "paused", true],
    [false, "paused", false],
    [false, "running", false],
    [true, "cancelled", false],
    [true, "success", false],
  ];
  for (const [canEdit, status, want] of cases) {
    assert.equal(canCancelRun(canEdit, status), want, `${canEdit} ${status}`);
  }
});
