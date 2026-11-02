#!/usr/bin/env node
// Build steps for the desktop app.
//   app:   compile main, preload and renderer into out/
//   stack: stage the backend, the standalone frontend and a pinned uv binary into stage/
//   dev:   build app, then rebuild on every save and run Electron (the app reloads itself)
//   start: build app once, then run Electron
// electron-builder copies stage/ into Skynet.app/Contents/Resources (see electron-builder.yml).
import { execFileSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";

const desktopDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(desktopDir, "..");
const outDir = path.join(desktopDir, "out");
const stageDir = path.join(desktopDir, "stage");
const pkg = JSON.parse(fs.readFileSync(path.join(desktopDir, "package.json"), "utf8"));

// uv is pinned by version and checksum; a release asset that changes upstream fails the build.
const UV_VERSION = "0.13.4";
const UV_ASSETS = {
  arm64: { triple: "aarch64-apple-darwin", sha256: "e0b1d4743ce64c55d9f0f4a444afa6b81125680070ee04e853fcd0f6c1ebf8f1" },
  x64: { triple: "x86_64-apple-darwin", sha256: "7be2dcd316122f4b3e5a0a7889f6248266c14425374e105ff7dc9779184603a6" },
};

const FONT_FILES = [
  "onest-latin-wght-normal.woff2",
  "heebo-hebrew-wght-normal.woff2",
  "heebo-latin-wght-normal.woff2",
  "bricolage-grotesque-latin-opsz-normal.woff2",
  "jetbrains-mono-latin-wght-normal.woff2",
  "OFL.txt",
];

function run(cmd, args, opts = {}) {
  execFileSync(cmd, args, { stdio: "inherit", ...opts });
}

function copyStatic() {
  const rendererOut = path.join(outDir, "renderer");
  fs.mkdirSync(rendererOut, { recursive: true });
  fs.copyFileSync(path.join(desktopDir, "src/renderer/index.html"), path.join(rendererOut, "index.html"));
  fs.copyFileSync(path.join(desktopDir, "src/renderer/onboarding.css"), path.join(rendererOut, "onboarding.css"));
  // The web app's font files are the source of truth; copy them instead of committing a second set.
  fs.mkdirSync(path.join(rendererOut, "fonts"), { recursive: true });
  for (const file of FONT_FILES) {
    fs.copyFileSync(path.join(repoRoot, "frontend/public/fonts", file), path.join(rendererOut, "fonts", file));
  }
  fs.mkdirSync(path.join(outDir, "icons"), { recursive: true });
  for (const file of ["dock-light.png", "dock-dark.png"]) {
    fs.copyFileSync(path.join(desktopDir, "build", file), path.join(outDir, "icons", file));
  }
}

function appBuilds() {
  const common = { bundle: true, sourcemap: false, logLevel: "warning", target: "es2023" };
  return [
    {
      ...common,
      entryPoints: [path.join(desktopDir, "src/main/index.ts")],
      outfile: path.join(outDir, "main/index.js"),
      platform: "node",
      format: "cjs",
      external: ["electron", "electron-updater"],
    },
    {
      ...common,
      entryPoints: [path.join(desktopDir, "src/preload/index.ts")],
      outfile: path.join(outDir, "preload/index.js"),
      platform: "node",
      format: "cjs",
      external: ["electron"],
    },
    {
      ...common,
      entryPoints: [path.join(desktopDir, "src/renderer/onboarding.ts")],
      outfile: path.join(outDir, "renderer/onboarding.js"),
      platform: "browser",
      format: "iife",
    },
  ];
}

async function buildApp() {
  fs.rmSync(outDir, { recursive: true, force: true });
  for (const options of appBuilds()) await esbuild.build(options);
  copyStatic();
  console.log(`built ${path.relative(desktopDir, outDir)}/`);
}

/**
 * Rebuild out/ on every save and run Electron once. With SKYNET_DESKTOP_DEV set,
 * the main process reloads the onboarding page when out/renderer changes and
 * relaunches itself (stopping the stack cleanly) when out/main or out/preload changes.
 */
async function dev() {
  await buildApp();
  for (const options of appBuilds()) await (await esbuild.context(options)).watch();
  for (const file of ["index.html", "onboarding.css"]) {
    fs.watch(path.join(desktopDir, "src/renderer", file), () => copyStatic());
  }
  spawn(electronBinary(), ["."], { cwd: desktopDir, stdio: "inherit", env: { ...process.env, SKYNET_DESKTOP_DEV: "1" } });
  console.log("watching src/ (Ctrl+C stops the watcher; quit the app with Cmd+Q)");
}

/** Build out/ once and run Electron, named Skynet in the Dock and menu bar. */
async function start() {
  await buildApp();
  spawn(electronBinary(), ["."], { cwd: desktopDir, stdio: "inherit" });
}

function electronBinary() {
  return process.platform === "darwin" ? devAppBinary() : path.join(desktopDir, "node_modules/.bin/electron");
}

/**
 * The Dock takes its hover name from the bundle folder and its menu bar name
 * from Info.plist, so dev runs a copy of Electron.app named Skynet.app.
 * Copied again only when the Electron version changes.
 */
function devAppBinary() {
  const source = path.join(desktopDir, "node_modules/electron/dist");
  const app = path.join(desktopDir, "dev-app/Skynet.app");
  const stamp = path.join(desktopDir, "dev-app/version");
  const version = fs.readFileSync(path.join(source, "version"), "utf8");
  if (!fs.existsSync(stamp) || fs.readFileSync(stamp, "utf8") !== version) {
    fs.rmSync(path.dirname(app), { recursive: true, force: true });
    execFileSync("ditto", [path.join(source, "Electron.app"), app]);
    const plist = path.join(app, "Contents/Info.plist");
    for (const [key, value] of [["CFBundleName", "Skynet"], ["CFBundleDisplayName", "Skynet"], ["CFBundleIdentifier", "com.skynet.desktop.dev"]]) {
      execFileSync("plutil", ["-replace", key, "-string", value, plist]);
    }
    fs.writeFileSync(stamp, version);
  }
  // skynet:// (SKY-228): macOS routes the scheme only to bundles that declare it,
  // and Launch Services must see the edited Info.plist. Runs on every start so
  // an existing dev copy gets it too.
  const plist = path.join(app, "Contents/Info.plist");
  const urlTypes = JSON.stringify([{ CFBundleURLName: "Skynet", CFBundleURLSchemes: ["skynet"] }]);
  execFileSync("plutil", ["-replace", "CFBundleURLTypes", "-json", urlTypes, plist]);
  execFileSync("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister", ["-f", app]);
  return path.join(app, "Contents/MacOS/Electron");
}

/** Copy the tracked backend files only, so .env files, .venv and caches never ship. */
function stageBackend() {
  const target = path.join(stageDir, "backend");
  const files = execFileSync("git", ["ls-files", "-z", "--", "backend"], { cwd: repoRoot, encoding: "utf8" })
    .split("\0")
    .filter(Boolean);
  for (const rel of files) {
    const src = path.join(repoRoot, rel);
    if (!fs.existsSync(src)) continue;
    const base = path.basename(rel);
    if (base === ".env" || (base.startsWith(".env.") && base !== ".env.example")) continue;
    const dest = path.join(target, path.relative("backend", rel));
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.copyFileSync(src, dest);
    fs.chmodSync(dest, fs.statSync(src).mode);
  }
  console.log(`staged backend (${files.length} files)`);
}

function stageFrontend() {
  const frontendDir = path.join(repoRoot, "frontend");
  const standalone = path.join(frontendDir, ".next/standalone");
  if (!fs.existsSync(path.join(standalone, "server.js")) || process.env.SKYNET_DESKTOP_REBUILD_FRONTEND === "1") {
    const env = { ...process.env, NEXT_TELEMETRY_DISABLED: "1" };
    if (!fs.existsSync(path.join(frontendDir, "node_modules"))) run("npm", ["ci", "--no-audit", "--no-fund"], { cwd: frontendDir, env });
    run("node", ["scripts/generate-third-party-licenses.mjs"], { cwd: frontendDir, env });
    run("npm", ["run", "build"], { cwd: frontendDir, env });
  }
  if (!fs.existsSync(path.join(standalone, "server.js"))) {
    throw new Error("frontend/.next/standalone/server.js is missing; next.config.ts must keep output: \"standalone\"");
  }
  const target = path.join(stageDir, "frontend");
  fs.cpSync(standalone, target, { recursive: true, verbatimSymlinks: true });
  fs.cpSync(path.join(frontendDir, ".next/static"), path.join(target, ".next/static"), { recursive: true });
  fs.cpSync(path.join(frontendDir, "public"), path.join(target, "public"), { recursive: true });
  // Next copies .env files into the standalone output; local secrets come from the desktop instead.
  for (const name of fs.readdirSync(target)) {
    if (name.startsWith(".env")) fs.rmSync(path.join(target, name), { force: true });
  }
  console.log("staged frontend");
}

function stageUv() {
  // electron-builder.yml always builds arm64 and x64, so each app needs its own uv.
  for (const arch of Object.keys(UV_ASSETS)) {
    const asset = UV_ASSETS[arch];
    if (!asset) throw new Error(`no uv asset for arch ${arch}`);
    const dest = path.join(stageDir, "uv", arch);
    fs.mkdirSync(dest, { recursive: true });
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "skynet-uv-"));
    const archive = path.join(tmp, "uv.tar.gz");
    const url = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${asset.triple}.tar.gz`;
    run("curl", ["-fsSL", "--retry", "3", "-o", archive, url]);
    const digest = createHash("sha256").update(fs.readFileSync(archive)).digest("hex");
    if (digest !== asset.sha256) throw new Error(`uv ${arch} checksum mismatch: ${digest}`);
    run("tar", ["-xzf", archive, "-C", tmp]);
    fs.copyFileSync(path.join(tmp, `uv-${asset.triple}`, "uv"), path.join(dest, "uv"));
    fs.chmodSync(path.join(dest, "uv"), 0o755);
    fs.rmSync(tmp, { recursive: true, force: true });
    console.log(`staged uv ${UV_VERSION} (${arch})`);
  }
}

function stageStack() {
  fs.rmSync(stageDir, { recursive: true, force: true });
  fs.mkdirSync(stageDir, { recursive: true });
  stageBackend();
  stageFrontend();
  stageUv();
  // The app copies the bundle into Application Support once per bundle id.
  let commit = "nogit";
  try {
    commit = execFileSync("git", ["rev-parse", "--short=12", "HEAD"], { cwd: repoRoot, encoding: "utf8" }).trim();
  } catch {
    // Source tarballs have no git metadata; the timestamp still makes the id unique.
  }
  fs.writeFileSync(path.join(stageDir, "bundle-id.txt"), `${pkg.version}-${commit}-${Date.now()}\n`);
}

const mode = process.argv[2];
if (mode === "app") await buildApp();
else if (mode === "stack") stageStack();
else if (mode === "dev") await dev();
else if (mode === "start") await start();
else {
  console.error("usage: bundle-resources.mjs app|stack|dev|start");
  process.exit(2);
}
