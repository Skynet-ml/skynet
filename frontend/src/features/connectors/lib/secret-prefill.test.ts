import assert from "node:assert/strict";
import { test } from "node:test";

import { secretNameFromSearch, withoutSecretParam } from "./secret-prefill.ts";

test("a request_secret link fills in a valid name only", () => {
  assert.equal(secretNameFromSearch("?settings=connectors&secret=MAPS_KEY"), "MAPS_KEY");
  for (const search of ["", "?secret=", "?secret=maps-key", "?secret=1KEY", "?secret=%3Cb%3E"]) {
    assert.equal(secretNameFromSearch(search), null, search);
  }
});

test("the secret parameter leaves the URL and the rest stays", () => {
  assert.equal(withoutSecretParam("https://app.example.test/runs?secret=MAPS_KEY&x=1#top"), "/runs?x=1#top");
});
