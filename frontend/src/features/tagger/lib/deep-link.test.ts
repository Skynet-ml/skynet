import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { opensTaggerSetup } from "./deep-link.ts";

test("a dataset or new deep link opens the tagger setup; a bare /tagger shows the chooser", () => {
  assert.equal(opensTaggerSetup(new URLSearchParams("dataset=abc-123")), true);
  assert.equal(opensTaggerSetup(new URLSearchParams("new=1")), true);
  assert.equal(opensTaggerSetup(new URLSearchParams("")), false);
  assert.equal(opensTaggerSetup(new URLSearchParams("tab=sessions")), false);
});

// The chat opens /tagger?dataset=<id> while the user may already be on /tagger.
// The page does not remount then, so the check must follow the URL, not run once.
test("the tagger view follows the URL's search params, not only its first mount", () => {
  const view = readFileSync(new URL("../components/TaggerView.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(view, /window\.location\.search/);
  assert.match(view, /const link = opensTaggerSetup\(params\) \? \(params\.get\("dataset"\) \?\? ""\) : null;/);
  assert.match(view, /useEffect\(\(\) => onChange\(link\), \[link, onChange\]\);/);
  assert.match(view, /if \(deepLink !== null\) setStartingNew\(true\);\n {2}\}, \[deepLink\]\);/);
  // The URL reader sits in its own Suspense, so the chooser still prerenders.
  assert.match(view, /<Suspense fallback=\{null\}>\n\s*<TaggerDeepLink onChange=\{setDeepLink\} \/>/);
  // A second dataset link while the setup is on view loads that dataset.
  assert.match(view, /\{deepLinkReader\}\n\s*\{backBar\}\n\s*<TaggerSetup key=\{deepLink \?\? ""\}/);
});
