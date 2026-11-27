import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createFolderIn, isAppPageSender, isValidFolderName, type FolderSender } from "../src/main/folders";

describe("isValidFolderName", () => {
  it("accepts a plain name", () => {
    for (const name of ["my-harness", "Harness 2", "a.b", "תיקייה", "x".repeat(255)]) {
      expect(isValidFolderName(name)).toBe(true);
    }
  });

  it("refuses empty names, separators, .. and other values", () => {
    for (const name of ["", " ", " lead", "trail ", ".", "..", "a..b", "../x", "a/b", "/abs", "a\\b", "a\0b", "x".repeat(256), "ת".repeat(128), null, undefined, 1, ["a"]]) {
      expect(isValidFolderName(name)).toBe(false);
    }
  });
});

describe("createFolderIn", () => {
  let tmp: string;
  let root: string;

  beforeEach(() => {
    tmp = fs.mkdtempSync(path.join(os.tmpdir(), "skynet-folders-"));
    root = path.join(tmp, "Documents", "Skynet");
  });

  afterEach(() => {
    fs.rmSync(tmp, { recursive: true, force: true });
  });

  it("creates the root and an empty folder, and returns its absolute path", () => {
    const created = createFolderIn(root, "harness");
    expect(created).toBe(path.join(root, "harness"));
    expect(fs.statSync(created!).isDirectory()).toBe(true);
    expect(fs.readdirSync(created!)).toEqual([]);
  });

  it("refuses an existing folder or file", () => {
    expect(createFolderIn(root, "harness")).not.toBeNull();
    fs.writeFileSync(path.join(root, "harness", "keep.txt"), "x");
    expect(createFolderIn(root, "harness")).toBeNull();
    expect(fs.readdirSync(path.join(root, "harness"))).toEqual(["keep.txt"]);
    fs.writeFileSync(path.join(root, "file"), "x");
    expect(createFolderIn(root, "file")).toBeNull();
  });

  it("refuses an invalid name and creates nothing outside the root", () => {
    for (const name of ["", "..", "../escape", "a/b"]) expect(createFolderIn(root, name)).toBeNull();
    expect(fs.existsSync(path.join(tmp, "Documents", "escape"))).toBe(false);
    expect(fs.existsSync(path.join(root, "a"))).toBe(false);
  });
});

describe("isAppPageSender", () => {
  const frontendUrl = "http://127.0.0.1:43123";
  const appPage: FolderSender = { fromAppWindow: true, fromMainFrame: true, frameUrl: "http://127.0.0.1:43123/optimizations/new" };

  it("accepts the main frame of the app window on the local web app origin", () => {
    expect(isAppPageSender(appPage, frontendUrl)).toBe(true);
  });

  it("refuses another window or an iframe", () => {
    expect(isAppPageSender({ ...appPage, fromAppWindow: false }, frontendUrl)).toBe(false);
    expect(isAppPageSender({ ...appPage, fromMainFrame: false }, frontendUrl)).toBe(false);
  });

  it("refuses other origins, the onboarding page and a missing URL", () => {
    for (const frameUrl of [
      "http://127.0.0.1:43124/",
      "http://localhost:43123/",
      "https://127.0.0.1:43123/",
      "https://skynetml.com/",
      "file:///Applications/Skynet.app/index.html",
      "not a url",
      "",
      undefined,
    ]) {
      expect(isAppPageSender({ ...appPage, frameUrl }, frontendUrl)).toBe(false);
    }
  });

  it("refuses every call before the web app runs", () => {
    expect(isAppPageSender(appPage, null)).toBe(false);
  });
});
