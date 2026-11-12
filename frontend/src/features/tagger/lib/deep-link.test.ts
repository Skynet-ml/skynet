import assert from "node:assert/strict";
import test from "node:test";

import { linkedDatasetName, opensTaggerSetup, startingNewAfterLink } from "./deep-link.ts";

test("a dataset or new deep link opens the tagger setup; a bare /tagger shows the chooser", () => {
  assert.equal(opensTaggerSetup(new URLSearchParams("dataset=abc-123")), true);
  assert.equal(opensTaggerSetup(new URLSearchParams("new=1")), true);
  assert.equal(opensTaggerSetup(new URLSearchParams("")), false);
  assert.equal(opensTaggerSetup(new URLSearchParams("tab=sessions")), false);
});

test("the setup follows the deep link as the URL changes", () => {
  // The chat opens /tagger?dataset=X while the chooser is on view.
  assert.equal(startingNewAfterLink(false, null, "X"), true);
  // ?new=1 opens the setup too.
  assert.equal(startingNewAfterLink(false, null, ""), true);
  // A second dataset link while the setup is on view keeps the setup.
  assert.equal(startingNewAfterLink(true, "X", "Y"), true);
  // Browser Back from /tagger?dataset=X to /tagger shows the chooser again.
  assert.equal(startingNewAfterLink(true, "X", null), false);
  assert.equal(startingNewAfterLink(true, "", null), false);
  // "Start new" on the chooser, with no link before or after, stays.
  assert.equal(startingNewAfterLink(true, null, null), true);
  assert.equal(startingNewAfterLink(false, null, null), false);
});

test("a linked dataset without a name in the link takes its name from the dataset list", () => {
  const datasets = [
    { id: "a", name: "Support tickets" },
    { id: "b", name: "Reviews: Q3 / EU" },
  ];
  assert.equal(linkedDatasetName("b", null, datasets), "Reviews: Q3 / EU");
  assert.equal(linkedDatasetName("a", "From the editor", datasets), "From the editor");
  assert.equal(linkedDatasetName("missing", null, datasets), null);
  assert.equal(linkedDatasetName("a", "", datasets), "Support tickets");
});
