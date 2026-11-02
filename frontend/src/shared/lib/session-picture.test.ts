import { test } from "node:test";
import assert from "node:assert/strict";
import { saveSessionPicture, savablePicture } from "./session-picture.ts";

const PICTURE = "https://lh3.googleusercontent.com/a/pia";

function recorder(ok = true) {
  const calls: [string, string][] = [];
  const save = async (email: string, image: string) => {
    calls.push([email, image]);
    return ok;
  };
  return { calls, save };
}

test("an old session picture goes to hosted once", async () => {
  const token = { email: "pia@example.test", picture: PICTURE };
  const { calls, save } = recorder();
  await saveSessionPicture(token, save);
  await saveSessionPicture(token, save);
  assert.deepEqual(calls, [["pia@example.test", PICTURE]]);
});

test("a new provider picture goes to hosted again", async () => {
  const token = { email: "pia@example.test", picture: PICTURE, pictureSaved: "https://old.test/a" };
  const { calls, save } = recorder();
  await saveSessionPicture(token, save);
  assert.deepEqual(calls, [["pia@example.test", PICTURE]]);
});

test("a failed save is tried again on the next read", async () => {
  const token = { email: "pia@example.test", picture: PICTURE };
  const failing = recorder(false);
  await saveSessionPicture(token, failing.save);
  const working = recorder();
  await saveSessionPicture(token, working.save);
  assert.equal(failing.calls.length, 1);
  assert.equal(working.calls.length, 1);
});

test("no picture, an unsafe picture or no email sends nothing", async () => {
  const { calls, save } = recorder();
  await saveSessionPicture({ email: "pia@example.test" }, save);
  await saveSessionPicture({ email: "pia@example.test", picture: "http://x.test/a.png" }, save);
  await saveSessionPicture(
    { email: "pia@example.test", picture: "https://x.test/" + "a".repeat(2048) },
    save,
  );
  await saveSessionPicture({ picture: PICTURE }, save);
  assert.equal(calls.length, 0);
});

test("savablePicture keeps only an https URL that fits the column", () => {
  assert.equal(savablePicture(PICTURE), PICTURE);
  assert.equal(savablePicture("javascript:alert(1)"), null);
  assert.equal(savablePicture(undefined), null);
});
