import assert from "node:assert/strict";
import { test } from "node:test";

import { cleanSecretHost, SECRET_HOST, secretNameFromSearch, withoutSecretParam } from "./secret-prefill.ts";

test("an approved host is an exact host or *.suffix, as the server requires", () => {
  assert.equal(cleanSecretHost(" Maps.GoogleAPIs.com. "), "maps.googleapis.com");
  for (const host of ["maps.googleapis.com", "*.example.com"]) assert.ok(SECRET_HOST.test(host), host);
  for (const host of ["", "*", "*.com", "localhost", "https://api.example.com", "api.example.com/x", "a b.com"]) {
    assert.equal(SECRET_HOST.test(cleanSecretHost(host)), false, host);
  }
});

test("a request_secret link fills in a valid name only", () => {
  assert.equal(secretNameFromSearch("?settings=connectors&secret=MAPS_KEY"), "MAPS_KEY");
  for (const search of ["", "?secret=", "?secret=maps-key", "?secret=1KEY", "?secret=%3Cb%3E"]) {
    assert.equal(secretNameFromSearch(search), null, search);
  }
});

test("the secret parameter leaves the URL and the rest stays", () => {
  assert.equal(withoutSecretParam("https://app.example.test/runs?secret=MAPS_KEY&x=1#top"), "/runs?x=1#top");
});
