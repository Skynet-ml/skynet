import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createFolderIn,
  isAppPageSender,
  isValidFolderName,
  MAX_PICKED_FOLDERS,
  PICKED_FOLDERS_FILE,
  recordPickedFolder,
  rememberPickedFolder,
  type FolderSender,
} from "../src/main/folders";

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

describe("recordPickedFolder", () => {
  let tmp: string;
  let stateDir: string;
  const read = (): string[] => JSON.parse(fs.readFileSync(path.join(stateDir, PICKED_FOLDERS_FILE), "utf8"));
  const folder = (name: string): string => {
    const dir = path.join(tmp, name);
    fs.mkdirSync(dir, { recursive: true });
    return dir;
  };

  beforeEach(() => {
    tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "skynet-picked-")));
    stateDir = path.join(tmp, "state");
  });

  afterEach(() => {
    fs.rmSync(tmp, { recursive: true, force: true });
  });

  it("creates the list and adds each folder once, newest last", () => {
    const a = folder("a");
    const b = folder("b");
    recordPickedFolder(stateDir, a);
    recordPickedFolder(stateDir, b);
    recordPickedFolder(stateDir, a);
    expect(read()).toEqual([b, a]);
    expect(fs.readdirSync(stateDir)).toEqual([PICKED_FOLDERS_FILE]);
  });

  it("stores the resolved path of a linked folder", () => {
    const real = folder("real");
    const link = path.join(tmp, "link");
    fs.symlinkSync(real, link);
    recordPickedFolder(stateDir, link);
    expect(read()).toEqual([real]);
  });

  it("keeps only the last 200 folders, and the list survives a restart", () => {
    const dirs = Array.from({ length: MAX_PICKED_FOLDERS + 5 }, (_, i) => folder(`f${i}`));
    for (const dir of dirs) recordPickedFolder(stateDir, dir);
    expect(MAX_PICKED_FOLDERS).toBe(200);
    expect(read()).toEqual(dirs.slice(-MAX_PICKED_FOLDERS));
    // Each call reads the list from disk, as a new app process does.
    recordPickedFolder(stateDir, folder("after-restart"));
    expect(read()).toHaveLength(MAX_PICKED_FOLDERS);
    expect(read().at(-1)).toBe(path.join(tmp, "after-restart"));
    expect(read()[0]).toBe(dirs[6]);
  });

  it("starts again from a damaged list", () => {
    fs.mkdirSync(stateDir, { recursive: true });
    fs.writeFileSync(path.join(stateDir, PICKED_FOLDERS_FILE), "{not json");
    const a = folder("a");
    recordPickedFolder(stateDir, a);
    expect(read()).toEqual([a]);
  });
});

describe("rememberPickedFolder", () => {
  let tmp: string;

  beforeEach(() => {
    tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "skynet-remember-")));
  });

  afterEach(() => {
    fs.rmSync(tmp, { recursive: true, force: true });
  });

  it("records a picked or created folder and returns it", () => {
    const stateDir = path.join(tmp, "state");
    const created = createFolderIn(path.join(tmp, "Skynet"), "my-app");
    const log = vi.fn();
    expect(rememberPickedFolder(stateDir, created, log)).toBe(created);
    expect(JSON.parse(fs.readFileSync(path.join(stateDir, PICKED_FOLDERS_FILE), "utf8"))).toEqual([created]);
    expect(log).not.toHaveBeenCalled();
  });

  it("returns nothing and records nothing for a cancelled pick", () => {
    const stateDir = path.join(tmp, "state");
    expect(rememberPickedFolder(stateDir, null, vi.fn())).toBeNull();
    expect(fs.existsSync(stateDir)).toBe(false);
  });

  it("still returns a created folder when the record fails, and logs the error", () => {
    // A file where the state folder must be makes the record fail.
    const stateDir = path.join(tmp, "state");
    fs.writeFileSync(stateDir, "");
    const created = createFolderIn(path.join(tmp, "Skynet"), "my-app");
    const log = vi.fn();
    expect(rememberPickedFolder(stateDir, created, log)).toBe(created);
    expect(log).toHaveBeenCalledOnce();
  });
});
