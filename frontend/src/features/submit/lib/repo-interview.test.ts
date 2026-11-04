/** Tests for the task interview's repository search helpers. */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { GithubRepository } from "@/shared/lib/api";

import {
  REPO_CONTEXT_PATH_MAX,
  REPO_CONTEXT_PATHS_MAX,
  REPO_CONTEXT_README_MAX,
  findRepo,
  parseGithubRepoRef,
  rankRepoMatches,
  readmePath,
  repoInterviewContext,
} from "./repo-interview.ts";

const repo = (full_name: string, description: string | null = null): GithubRepository => ({
  full_name,
  private: false,
  description,
  language: null,
  default_branch: "main",
  pushed_at: null,
});

test("parseGithubRepoRef reads links and bare names", () => {
  assert.equal(parseGithubRepoRef("https://github.com/me/site"), "me/site");
  assert.equal(parseGithubRepoRef("https://github.com/me/site.git"), "me/site");
  assert.equal(parseGithubRepoRef("http://www.github.com/Me/my.site/tree/main/src"), "Me/my.site");
  assert.equal(parseGithubRepoRef("github.com/vercel/next.js?tab=readme"), "vercel/next.js");
  assert.equal(parseGithubRepoRef("git@github.com:me/site.git"), "me/site");
  assert.equal(parseGithubRepoRef("  me/site "), "me/site");
  assert.equal(parseGithubRepoRef("my personal website"), null);
  assert.equal(parseGithubRepoRef("https://gitlab.com/me/site"), null);
  assert.equal(parseGithubRepoRef(""), null);
});

test("rankRepoMatches puts exact and prefix names first and caps the list", () => {
  const repos = [
    repo("me/blog"),
    repo("me/personal-website", "My site"),
    repo("org/website-tools"),
    repo("me/website"),
    repo("me/notes", "Notes for my personal website"),
  ];
  assert.deepEqual(
    rankRepoMatches(repos, "website").map((r) => r.full_name),
    ["me/website", "org/website-tools", "me/personal-website", "me/notes"],
  );
  assert.deepEqual(
    rankRepoMatches(repos, "personal website").map((r) => r.full_name),
    ["me/personal-website", "me/notes"],
  );
  assert.deepEqual(rankRepoMatches(repos, "zzz"), []);
  const many = Array.from({ length: 10 }, (_, i) => repo(`me/site-${i}`));
  assert.equal(rankRepoMatches(many, "site").length, 6);
  assert.deepEqual(
    rankRepoMatches(many, "").map((r) => r.full_name),
    many.slice(0, 6).map((r) => r.full_name),
  );
});

test("findRepo ignores case", () => {
  assert.equal(findRepo([repo("Me/Site")], "me/site")?.full_name, "Me/Site");
  assert.equal(findRepo([repo("me/site")], "me/other"), null);
});

test("readmePath prefers a root README.md and ignores nested ones", () => {
  assert.equal(
    readmePath([
      { path: "docs/README.md", type: "file" },
      { path: "readme.rst", type: "file" },
      { path: "README.md", type: "file" },
    ]),
    "README.md",
  );
  assert.equal(readmePath([{ path: "Readme.txt", type: "file" }]), "Readme.txt");
  assert.equal(readmePath([{ path: "docs/README.md", type: "file" }]), null);
  assert.equal(readmePath([{ path: "README", type: "dir" }]), null);
});

test("repoInterviewContext cuts the README and the paths and flags the cut", () => {
  const entries = [
    { path: "src", type: "dir" as const },
    ...Array.from({ length: REPO_CONTEXT_PATHS_MAX + 5 }, (_, i) => ({
      path: `src/f${i}.ts`,
      type: "file" as const,
    })),
  ];
  const big = repoInterviewContext(
    "me/site",
    "x".repeat(REPO_CONTEXT_README_MAX + 1),
    entries,
    false,
  );
  assert.equal(big.readme.length, REPO_CONTEXT_README_MAX);
  assert.equal(big.paths.length, REPO_CONTEXT_PATHS_MAX);
  assert.equal(big.paths[0], "src/f0.ts");
  assert.equal(big.truncated, true);
  const small = repoInterviewContext("me/site", "", [{ path: "index.html", type: "file" }], false);
  assert.deepEqual(small, {
    repository: "me/site",
    readme: "",
    paths: ["index.html"],
    truncated: false,
  });
  assert.equal(repoInterviewContext("me/site", "", [], true).truncated, true);
});

test("repoInterviewContext leaves out paths the server would refuse", () => {
  const long = `src/${"x".repeat(REPO_CONTEXT_PATH_MAX)}.ts`;
  const context = repoInterviewContext(
    "me/site",
    "",
    [
      { path: long, type: "file" },
      { path: "index.html", type: "file" },
    ],
    false,
  );
  assert.deepEqual(context.paths, ["index.html"]);
});
