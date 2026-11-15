import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const lineDiffUrl = new URL("../../../../shared/lib/line-diff.ts", import.meta.url).href;
const diffRowsUrl = new URL("../../../../shared/ui/repo-files/diff-rows.ts", import.meta.url).href;
const draftChangesUrl = new URL("../draft-changes.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/lib/line-diff") return { shortCircuit: true, url: lineDiffUrl };
    if (specifier === "@/shared/ui/repo-files/diff-rows") {
      return { shortCircuit: true, url: diffRowsUrl };
    }
    if (specifier === "../draft-changes") return { shortCircuit: true, url: draftChangesUrl };
    return nextResolve(specifier, context);
  },
});

const { diffLines, statusOf, turnView } = await import("./lib.ts");

const files = [
  { path: "a.py", before: "x = 0\n", after: "x = 2\n" },
  { path: "b.py", before: "old\n", after: "new\n" },
  { path: "new.py", before: "", after: "z\n" },
  { path: "kept.py", before: "", after: "", existed: true },
  { path: "tree.py", before: "t\n", after: "t\n" },
];

test("the last turn diffs each file it changed from its text before the turn", () => {
  const view = turnView(
    files,
    new Map([
      ["a.py", { before: "x = 1\n", after: "x = 2\n" }],
      ["new.py", { before: null, after: "z\n" }],
      ["kept.py", { before: "", after: "" }],
    ]),
  );
  const byPath = new Map(view.map((f) => [f.path, f]));
  assert.equal(byPath.get("a.py")!.before, "x = 1\n");
  assert.equal(statusOf(byPath.get("a.py")!), "modified");
  // The draft did not hold new.py before the turn: it diffs from the repository.
  assert.equal(byPath.get("new.py"), files[2]);
  assert.equal(statusOf(byPath.get("new.py")!), "added");
  // The turn did not change b.py, so its earlier draft change does not show.
  assert.equal(statusOf(byPath.get("b.py")!), "unchanged");
  assert.equal(byPath.get("tree.py"), files[4]);
});

test("a file the draft held before the turn is not new, even when it was empty", () => {
  const [view] = turnView(
    [{ path: "run.py", before: "", after: "run()\n" }],
    new Map([["run.py", { before: "", after: "run()\n" }]]),
  );
  assert.equal(statusOf(view!), "modified");
});

test("a file's view keeps its object while the file and the turn stay the same", () => {
  const turn = new Map([["a.py", { before: "x = 1\n", after: "x = 2\n" }]]);
  const first = turnView(files, turn);
  const second = turnView(files, new Map(turn));
  first.forEach((f, i) => assert.equal(second[i], f));
  const edited = turnView([{ ...files[1]!, after: "newer\n" }], turn);
  assert.notEqual(edited[0], first[1]);
  assert.equal(edited[0]!.after, "newer\n");
});

test("a user edit after the turn does not show in the turn's diff", () => {
  const lines = (first: string, tenth: string) =>
    [first, "2", "3", "4", "5", "6", "7", "8", "9", tenth, ""].join("\n");
  const start = lines("one", "ten");
  const end = lines("ONE", "ten");
  // The user edits line 10 after the turn changed line 1.
  const [view] = turnView(
    [{ path: "a.py", before: start, after: lines("ONE", "TEN") }],
    new Map([["a.py", { before: start, after: end }]]),
  );
  assert.equal(view!.before, start);
  assert.equal(view!.after, end);
  const changed = diffLines(view!.before, view!.after).filter((row) => row.kind !== "same");
  assert.deepEqual(
    changed.map((row) => row.text),
    ["one", "ONE"],
  );
});
