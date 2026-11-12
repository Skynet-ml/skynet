import assert from "node:assert/strict";
import { test } from "node:test";

import type { RunFolder } from "./api.ts";
import { canLowerToViewer, topFolderName } from "./shared-with-me.ts";

function folder(id: string, name: string, parentId: string | null): RunFolder {
  return {
    id,
    name,
    parent_id: parentId,
    owner: "alice",
    role: "viewer",
    shared: true,
    editors_can_share: true,
    run_count: 0,
  };
}

test("only an editor can lower their own role", () => {
  assert.equal(canLowerToViewer("editor"), true);
  assert.equal(canLowerToViewer("viewer"), false);
  assert.equal(canLowerToViewer("owner"), false);
  assert.equal(canLowerToViewer(null), false);
});

test("a nested folder points to the top shared folder", () => {
  const folders = [folder("a", "Team", null), folder("b", "Sub", "a"), folder("c", "Deep", "b")];
  assert.equal(topFolderName(folders, "c"), "Team");
  assert.equal(topFolderName(folders, "a"), "Team");
});

test("an unknown folder or a parent cycle does not loop", () => {
  assert.equal(topFolderName([], "x"), null);
  const cycle = [folder("a", "A", "b"), folder("b", "B", "a")];
  assert.ok(["A", "B"].includes(topFolderName(cycle, "a") ?? ""));
});
