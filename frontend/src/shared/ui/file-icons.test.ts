import assert from "node:assert/strict";
import test from "node:test";

import { fileIcon, fileIconId, FOLDER_ICON, FOLDER_OPEN_ICON } from "./file-icons.ts";

test("an exact file name wins over its extension", () => {
  assert.equal(fileIconId("requirements.txt"), "python-misc");
  assert.equal(fileIconId("package.json"), "nodejs");
  assert.equal(fileIconId("pyproject.toml"), "python-misc");
  assert.equal(fileIconId("Dockerfile"), "docker");
  assert.equal(fileIconId("README.md"), "readme");
  assert.equal(fileIconId("LICENSE"), "license");
});

test("paths and case do not change the match", () => {
  assert.equal(fileIconId("backend/core/app.py"), "python");
  assert.equal(fileIconId("src/App.TSX"), "react_ts");
  assert.equal(fileIconId("frontend/package.json"), "nodejs");
});

test("the longest extension matches first, and dotfiles match their extension", () => {
  assert.equal(fileIconId("types.d.ts"), "typescript-def");
  assert.equal(fileIconId("main.ts"), "typescript");
  assert.equal(fileIconId(".env"), "tune");
  assert.equal(fileIconId("archive.tar.gz"), "zip");
});

test("an unknown or missing extension falls back to the plain file icon", () => {
  assert.equal(fileIconId("notes.unknownext"), "file");
  assert.equal(fileIconId("CODEOWNERS"), "file");
  assert.equal(fileIconId(""), "file");
});

test("every icon is an SVG data URI", () => {
  for (const icon of [fileIcon("a.py"), fileIcon("a.toml"), FOLDER_ICON, FOLDER_OPEN_ICON]) {
    assert.match(icon.src, /^data:image\/svg\+xml,%3Csvg /);
  }
  assert.match(fileIcon("a.toml").lightSrc ?? "", /^data:image\/svg\+xml,/);
});
