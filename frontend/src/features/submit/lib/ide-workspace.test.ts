import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FIXED_FILE_ISSUE,
  WorkspaceFs,
  WorkspaceFsError,
  diffWorkspace,
  explorerEditIssue,
  fixedFileIssue,
  workspacePathOf,
  workspaceTexts,
  type WorkspaceFsChange,
} from "./ide-workspace.ts";

// The wizard's rules in small: a .py or .md file, at most four levels, no
// taken name, no file where a folder is.
function pathIssue(path: string, files: Readonly<Record<string, string>>): string | null {
  if (path.split("/").length > 4) return "depth";
  if (!/\.(py|md)$/.test(path)) return "suffix";
  if (["run.py", "metric.py"].includes(path.split("/")[0] ?? "")) return "reserved";
  if (path in files) return "submit.v2.metric.files.error.exists";
  return null;
}

function setup(texts: Record<string, string> = workspaceTexts("R", "M", { "lib/parse.py": "P" })) {
  const changes: WorkspaceFsChange[][] = [];
  const edits: Record<string, string>[] = [];
  const fs = new WorkspaceFs(
    pathIssue,
    (batch) => changes.push(batch),
    (snapshot) => edits.push(snapshot),
  );
  fs.load(texts);
  changes.length = 0;
  return { fs, changes, edits };
}

function refusal(fn: () => void): WorkspaceFsError {
  try {
    fn();
  } catch (error) {
    assert.ok(error instanceof WorkspaceFsError);
    return error;
  }
  assert.fail("expected a refusal");
}

test("workspaceTexts puts run.py and metric.py beside the extra files", () => {
  assert.deepEqual(workspaceTexts("r", "m", { "a.py": "a" }), {
    "a.py": "a",
    "run.py": "r",
    "metric.py": "m",
  });
});

test("diffWorkspace lists changed and new files as writes and gone files as deletes", () => {
  const diff = diffWorkspace(
    { "run.py": "r", "a.py": "a", "b.py": "b" },
    { "run.py": "r2", "a.py": "a", "c.py": "c" },
  );
  assert.deepEqual(diff, {
    writes: [
      ["run.py", "r2"],
      ["c.py", "c"],
    ],
    deletes: ["b.py"],
  });
});

test("workspacePathOf maps the IDE root and refuses paths outside it", () => {
  assert.equal(workspacePathOf("/workspace"), "");
  assert.equal(workspacePathOf("/workspace/lib/a.py"), "lib/a.py");
  assert.equal(workspacePathOf("/elsewhere/a.py"), null);
  assert.equal(workspacePathOf("/workspaces/a.py"), null);
});

test("load lays the files out as folders", () => {
  const { fs } = setup();
  assert.deepEqual(fs.readdir(""), [
    ["lib", "folder"],
    ["metric.py", "file"],
    ["run.py", "file"],
  ]);
  assert.deepEqual(fs.readdir("lib"), [["parse.py", "file"]]);
  assert.equal(fs.read("lib/parse.py"), "P");
  assert.equal(fs.stat("lib").kind, "folder");
});

test("load reports the wizard's changes as file events but never as an edit", () => {
  const { fs, changes, edits } = setup();
  fs.load(workspaceTexts("R2", "M", { "lib/parse.py": "P", "util/x.py": "X" }));
  assert.deepEqual(changes.flat(), [
    { kind: "added", path: "util" },
    { kind: "added", path: "util/x.py" },
    { kind: "changed", path: "run.py" },
  ]);
  fs.load(workspaceTexts("R2", "M", {}));
  assert.deepEqual(changes.at(-1), [
    { kind: "deleted", path: "lib/parse.py" },
    { kind: "deleted", path: "util/x.py" },
  ]);
  assert.equal(edits.length, 0);
});

test("loading the same files again changes nothing, so the two-way sync settles", () => {
  const { fs, changes } = setup();
  fs.load(fs.snapshot());
  assert.equal(changes.length, 0);
});

test("a write from the IDE reports the whole workspace back", () => {
  const { fs, edits } = setup();
  fs.write("run.py", "R2", { create: false, overwrite: true });
  fs.write("lib/new.py", "N", { create: true, overwrite: false });
  assert.deepEqual(edits.at(-1), {
    "lib/parse.py": "P",
    "run.py": "R2",
    "metric.py": "M",
    "lib/new.py": "N",
  });
  assert.deepEqual(diffWorkspace(edits[0]!, edits[1]!), {
    writes: [["lib/new.py", "N"]],
    deletes: [],
  });
});

test("a new file must pass the workspace's path rules", () => {
  const { fs } = setup();
  assert.equal(
    refusal(() => fs.write("notes.bin", "", { create: true, overwrite: false })).issue,
    "suffix",
  );
  assert.equal(
    refusal(() => fs.write("a/b/c/d/e.py", "", { create: true, overwrite: false })).issue,
    "depth",
  );
  assert.equal(
    refusal(() => fs.write("run.py", "", { create: true, overwrite: false })).code,
    "exists",
  );
});

test("an empty folder lives in the IDE only until a file goes in it", () => {
  const { fs, edits } = setup();
  fs.mkdir("pkg/sub");
  assert.deepEqual(fs.readdir("pkg"), [["sub", "folder"]]);
  assert.equal(edits.length, 0);
  fs.write("pkg/sub/m.py", "", { create: true, overwrite: false });
  assert.ok("pkg/sub/m.py" in edits.at(-1)!);
});

test("deleting an empty folder leaves the wizard's files alone", () => {
  const { fs, changes, edits } = setup();
  fs.mkdir("empty");
  fs.delete("empty");
  assert.deepEqual(changes.at(-1), [{ kind: "deleted", path: "empty" }]);
  assert.equal(edits.length, 0);
});

test("deleting a folder drops every file under it", () => {
  const { fs, edits } = setup(
    workspaceTexts("R", "M", { "lib/a.py": "A", "lib/deep/b.py": "B", "c.py": "C" }),
  );
  fs.delete("lib");
  assert.deepEqual(Object.keys(edits.at(-1)!).sort(), ["c.py", "metric.py", "run.py"]);
  assert.equal(refusal(() => fs.stat("lib/deep")).code, "not-found");
});

test("renaming or moving a folder carries its files", () => {
  const { fs, edits } = setup(workspaceTexts("R", "M", { "lib/a.py": "A", "lib/deep/b.py": "B" }));
  fs.rename("lib", "pkg/lib", { overwrite: false });
  assert.deepEqual(edits.at(-1), {
    "run.py": "R",
    "metric.py": "M",
    "pkg/lib/a.py": "A",
    "pkg/lib/deep/b.py": "B",
  });
  fs.rename("pkg/lib/a.py", "a.py", { overwrite: false });
  assert.equal(fs.read("a.py"), "A");
});

test("a move that would break the path rules is refused and leaves the files alone", () => {
  const { fs, edits } = setup(workspaceTexts("R", "M", { "lib/deep/b.py": "B" }));
  assert.equal(refusal(() => fs.rename("lib", "x/y/z/lib", { overwrite: false })).issue, "depth");
  assert.equal(refusal(() => fs.rename("lib", "lib/inner", { overwrite: false })).code, "refused");
  assert.equal(fs.read("lib/deep/b.py"), "B");
  assert.equal(edits.length, 0);
});

test("run.py and metric.py cannot be deleted, renamed, moved or replaced", () => {
  const { fs, edits } = setup();
  for (const op of [
    () => fs.delete("run.py"),
    () => fs.delete("metric.py"),
    () => fs.rename("run.py", "main.py", { overwrite: false }),
    () => fs.rename("metric.py", "lib/metric.py", { overwrite: false }),
    () => fs.rename("lib/parse.py", "run.py", { overwrite: true }),
    () => fs.delete(""),
  ]) {
    const error = refusal(op);
    assert.equal(error.code, "refused");
    assert.equal(error.issue, FIXED_FILE_ISSUE);
  }
  assert.equal(fs.read("run.py"), "R");
  assert.equal(fs.read("metric.py"), "M");
  assert.equal(edits.length, 0);
});

test("fixedFileIssue lets every other path through", () => {
  assert.equal(fixedFileIssue("lib/run.py"), null);
  assert.equal(fixedFileIssue("lib", "pkg/lib"), null);
  assert.equal(fixedFileIssue("run.py"), FIXED_FILE_ISSUE);
  assert.equal(fixedFileIssue("a.py", "metric.py"), FIXED_FILE_ISSUE);
});

test("explorerEditIssue refuses an explorer delete or rename that touches a fixed file", () => {
  assert.equal(explorerEditIssue(["/workspace/lib/parse.py", "/workspace/notes.md"]), null);
  assert.equal(
    explorerEditIssue(["/workspace/lib/parse.py", "/workspace/run.py"]),
    FIXED_FILE_ISSUE,
  );
  assert.equal(explorerEditIssue(["/workspace/metric.py"]), FIXED_FILE_ISSUE);
  assert.equal(explorerEditIssue(["/workspace"]), FIXED_FILE_ISSUE);
  // Outside the workspace is the provider's business (not found), not a refusal.
  assert.equal(explorerEditIssue(["/elsewhere/run.py"]), null);
  assert.equal(explorerEditIssue([]), null);
});
