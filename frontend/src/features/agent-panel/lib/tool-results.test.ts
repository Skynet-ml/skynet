import assert from "node:assert/strict";
import { test } from "node:test";

const {
  excerpt,
  flatPairs,
  githubFile,
  githubItem,
  githubRows,
  hostOf,
  jobRows,
  logLines,
  numberedFile,
  openedTarget,
  plainLines,
  registryGroups,
  repoTree,
  settingRows,
  setupCheck,
  statRows,
  statusCounts,
  taggingGuide,
  webLinks,
  linkHref,
  stripTruncated,
  webPages,
  workspaceSave,
  writeCounts,
  writtenLines,
} = await import("./tool-results.ts");

void test("a host drops www. and a bad URL gives an empty host", () => {
  assert.equal(hostOf("https://www.example.com/a?b=1"), "example.com");
  assert.equal(hostOf("https://docs.python.org/3/"), "docs.python.org");
  assert.equal(hostOf("/docs/getting-started"), "");
});

void test("an excerpt strips markdown syntax and cuts long text", () => {
  assert.equal(
    excerpt("# Title\n\nSee [the docs](https://x.y) and **bold** `code`.\n\n![img](a.png)"),
    "Title See the docs and bold code.",
  );
  const long = excerpt("word ".repeat(100), 20);
  assert.ok(long.length <= 20);
  assert.ok(long.endsWith("…"));
});

void test("web search links read from a bare list, docs links from results", () => {
  assert.deepEqual(
    webLinks([{ title: "Python", url: "https://www.python.org/", description: "The **site**" }]),
    [{ title: "Python", url: "https://www.python.org/", host: "python.org", snippet: "The site" }],
  );
  assert.deepEqual(
    webLinks('{"query":"q","results":[{"title":"Start","href":"/docs/start","text":"Hi"}]}'),
    [{ title: "Start", url: "/docs/start", host: "", snippet: "Hi" }],
  );
  assert.deepEqual(webLinks([]), []);
  assert.equal(webLinks("Search failed: timeout"), null);
});

void test("a fetched page and a crawl read as pages", () => {
  const one = webPages({
    url: "https://a.com/x",
    title: "X",
    markdown: "Hello *world*",
    truncated: true,
  });
  assert.deepEqual(one, {
    pages: [
      {
        title: "X",
        url: "https://a.com/x",
        host: "a.com",
        excerpt: "Hello world",
        truncated: true,
      },
    ],
    partial: false,
    budget: false,
  });
  const crawl = webPages({
    url: "https://a.com",
    status: "partial",
    pages: [{ url: "https://a.com/1", title: "One", markdown: "" }, { title: "no url" }, {}],
  });
  assert.equal(crawl?.partial, true);
  assert.equal(crawl?.budget, false);
  // A crawl that used up its character budget is not the same as one that did not finish.
  const full = webPages({ status: "completed", truncated: true, pages: [{ title: "A" }] });
  assert.equal(full?.partial, false);
  assert.equal(full?.budget, true);
  // A page with no URL stays (its title still reads); an empty entry drops.
  assert.deepEqual(
    crawl?.pages.map((p) => [p.url, p.title]),
    [
      ["https://a.com/1", "One"],
      ["", "no url"],
    ],
  );
  assert.equal(webPages("Could not fetch"), null);
});

void test("a numbered file reads its lines and the cut mark; a note stays a note", () => {
  assert.deepEqual(numberedFile("   1 import os\n   2 \n  10 x = 1\n... (file cut here)"), {
    kind: "file",
    lines: [
      { n: 1, text: "import os" },
      { n: 2, text: "" },
      { n: 10, text: "x = 1" },
    ],
    cut: true,
  });
  assert.deepEqual(numberedFile("No file named 'a.py' in the repository."), {
    kind: "note",
    text: "No file named 'a.py' in the repository.",
  });
  assert.deepEqual(numberedFile(""), { kind: "file", lines: [], cut: false });
  assert.equal(numberedFile({ a: 1 }), null);
  assert.equal(numberedFile({ error: "No file named 'a.py' in the repository." }), null);
});

void test("plain lines count from one and drop the last newline", () => {
  assert.deepEqual(plainLines("a\nb\n"), [
    { n: 1, text: "a" },
    { n: 2, text: "b" },
  ]);
  assert.deepEqual(plainLines(""), []);
});

void test("a folder tree reads folders, files, marks and folds", () => {
  const view = repoTree(
    "src/  [editable]\n  main.py\n  util/ (4 files)\n  ... +3 more files\nREADME.md\n... (tree cut here; call list_repo_folder to see more)",
  );
  assert.equal(view?.kind, "tree");
  if (view?.kind !== "tree") return;
  assert.equal(view.cut, true);
  assert.deepEqual(view.entries, [
    { name: "src", depth: 0, dir: true, files: null, editable: true, more: null },
    { name: "main.py", depth: 1, dir: false, files: null, editable: false, more: null },
    { name: "util", depth: 1, dir: true, files: 4, editable: false, more: null },
    { name: "", depth: 1, dir: false, files: null, editable: false, more: 3 },
    { name: "README.md", depth: 0, dir: false, files: null, editable: false, more: null },
  ]);
});

void test("the root overview keeps only the tree and the names of the files it read", () => {
  const view = repoTree(
    [
      "Repository a/b on branch main.",
      "The optimizer may change: src.",
      "Tree ([editable] marks what the optimizer may change):",
      "src/",
      "  app.py",
      "",
      "--- README.md (already read for you) ---",
      "# Hello",
      "",
      "--- pyproject.toml (already read for you) ---",
      "[project]",
    ].join("\n"),
  );
  assert.equal(view?.kind, "tree");
  if (view?.kind !== "tree") return;
  assert.deepEqual(
    view.entries.map((e) => e.name),
    ["src", "app.py"],
  );
  assert.deepEqual(view.keyFiles, ["README.md", "pyproject.toml"]);
});

void test("a README line in the key-file header's form is not a file the overview read", () => {
  const view = repoTree(
    [
      "Repository a/b on branch main.",
      "Tree ([editable] marks what the optimizer may change):",
      "src/",
      "",
      "--- README.md (already read for you) ---",
      "# Hello",
      "--- secret.txt (already read for you) ---",
    ].join("\n"),
  );
  assert.equal(view?.kind, "tree");
  if (view?.kind !== "tree") return;
  assert.deepEqual(view.keyFiles, ["README.md"]);
});

void test("a failed folder read is not a tree; the empty-folder note is an empty tree", () => {
  assert.equal(repoTree({ error: "No folder named 'x' in the repository." }), null);
  assert.deepEqual(repoTree({ note: "The folder is empty." }), {
    kind: "tree",
    entries: [],
    keyFiles: [],
    cut: false,
  });
  assert.equal(repoTree({ note: "x", error: "y" }), null);
  assert.equal(repoTree({ note: 1 }), null);
});

void test("an old saved repo read with a null result is no result, not a file or a tree", () => {
  assert.deepEqual(numberedFile(null), { kind: "none" });
  assert.deepEqual(repoTree(null), { kind: "none" });
  assert.deepEqual(numberedFile(undefined), { kind: "none" });
  assert.deepEqual(repoTree(undefined), { kind: "none" });
  // A file whose text is "null" still reads as a file.
  assert.deepEqual(numberedFile("   1 null"), {
    kind: "file",
    lines: [{ n: 1, text: "null" }],
    cut: false,
  });
});

void test("the root overview marks the tree cut when GitHub cut it short", () => {
  const overview = (truncated: boolean) =>
    repoTree(
      [
        "Repository a/b on branch main.",
        "Tree ([editable] marks what the optimizer may change):",
        "src/",
        ...(truncated ? ["GitHub cut the tree short, so some files are missing above."] : []),
        "",
        "--- README.md (already read for you) ---",
        "# Hello",
      ].join("\n"),
    );
  assert.equal(overview(true)?.cut, true);
  assert.deepEqual(
    overview(true)?.entries.map((e) => e.name),
    ["src"],
  );
  assert.equal(overview(false)?.cut, false);
});

void test("a workspace save reads the saved file and the toml error; a refusal is a note", () => {
  assert.deepEqual(
    workspaceSave({
      wizard_state: { optimize_toml: "[run]\n" },
      optimize_toml_error: { section: "run", key: "entry", message: "[run] entry is missing" },
    }),
    {
      text: "[run]\n",
      tomlError: { section: "run", key: "entry", message: "[run] entry is missing" },
    },
  );
  assert.deepEqual(workspaceSave({ wizard_state: { metric_code: "x" } }), {
    text: "x",
    tomlError: null,
  });
  assert.deepEqual(workspaceSave("Not a file you can write: 'a'."), {
    note: "Not a file you can write: 'a'.",
  });
  assert.equal(workspaceSave({ other: 1 }), null);
});

void test("write counts add the file; edit counts swap the old lines for the new", () => {
  assert.deepEqual(writeCounts("write_workspace_file", { text: "a\nb\nc\n" }), {
    added: 3,
    removed: 0,
  });
  assert.deepEqual(writeCounts("edit_workspace_file", { old_text: "a", new_text: "b\nc" }), {
    added: 2,
    removed: 1,
  });
  assert.deepEqual(writeCounts("edit_workspace_file", {}), { added: 0, removed: 0 });
});

void test("GitHub search rows read the items and the total", () => {
  const out = githubRows(
    "search_repositories",
    '{"total_count":120,"items":[{"full_name":"a/b","description":"Lib","language":"Go","stargazers_count":5,"html_url":"https://github.com/a/b","private":true}]}',
  );
  assert.equal(out?.total, 120);
  assert.deepEqual(out?.rows[0], {
    key: "a/b",
    title: "a/b",
    subtitle: "Lib",
    meta: ["Go", "★ 5"],
    sha: "",
    href: "https://github.com/a/b",
    flags: ["private"],
  });
});

void test("GitHub commit, branch, release and directory rows read their main fields", () => {
  const commits = githubRows("list_commits", [
    {
      sha: "abcdef1234",
      html_url: "https://github.com/a/b/commit/abcdef1234",
      commit: {
        message: "Fix it\n\nLong body",
        author: { name: "Dana", date: "2026-01-02T03:04:05Z" },
      },
      author: { login: "dana" },
    },
  ]);
  assert(commits?.kind === "rows");
  assert.deepEqual(commits.rows[0]?.title, "Fix it");
  assert.deepEqual(commits.rows[0]?.meta, ["dana", "2026-01-02"]);
  assert.equal(commits.rows[0]?.sha, "abcdef1");
  const branches = githubRows("list_branches", [
    { name: "main", commit: { sha: "1234567890" }, protected: true },
  ]);
  assert(branches?.kind === "rows");
  assert.deepEqual(branches.rows[0]?.flags, ["protected"]);
  assert.equal(branches.rows[0]?.sha, "1234567");
  const releases = githubRows("list_releases", [
    { tag_name: "v1", name: "One", prerelease: true, published_at: "2026-02-03T00:00:00Z" },
  ]);
  assert(releases?.kind === "rows");
  assert.deepEqual(releases.rows[0]?.meta, ["v1", "2026-02-03"]);
  assert.deepEqual(releases.rows[0]?.flags, ["prerelease"]);
  const dir = githubRows("get_file_contents", [{ name: "src", path: "src", type: "dir" }]);
  assert(dir?.kind === "rows");
  assert.deepEqual(dir.rows[0]?.flags, ["dir"]);
  assert.equal(githubRows("list_commits", ""), null);
  assert.equal(githubRows("get_commit", []), null);
});

void test("a GitHub commit item reads its stats and files", () => {
  const item = githubItem("get_commit", {
    sha: "abcdef1234",
    commit: { message: "Subject\n\nBody line", author: { name: "Dana" } },
    stats: { additions: 3, deletions: 1 },
    files: [{ filename: "a.py", status: "modified", additions: 3, deletions: 1 }],
  });
  assert(item?.kind === "item");
  assert.equal(item.title, "Subject");
  assert.equal(item.subtitle, "Body line");
  assert.equal(item.added, 3);
  assert.deepEqual(item.files, [{ path: "a.py", status: "modified", added: 3, removed: 1 }]);
  const release = githubItem("get_latest_release", { tag_name: "v2" });
  assert(release?.kind === "item");
  assert.equal(release.title, "v2");
  assert.deepEqual(githubItem("get_commit", "not json"), {
    kind: "text",
    text: "not json",
    cut: false,
  });
  assert.equal(githubItem("get_commit", ""), null);
});

void test("a GitHub file drops its status line and the truncation note", () => {
  assert.deepEqual(githubFile("successfully downloaded text file (SHA: 1)\n\nline 1\nline 2"), {
    body: "line 1\nline 2",
    cut: false,
    binary: false,
  });
  assert.deepEqual(
    githubFile(
      "successfully downloaded text file (SHA: 1)\n\nbody\n\n[Truncated: the result has 50000 characters and only the first 40000 are shown]",
    ),
    { body: "body", cut: true, binary: false },
  );
  assert.equal(githubFile('[{"name":"a"}]'), null);
});

void test("log lines keep the message, the level and the time", () => {
  assert.deepEqual(
    logLines([
      { timestamp: "2026-01-01T00:00:00Z", level: "warning", message: "slow", logger: "x" },
      { level: "info" },
    ]),
    [{ time: "2026-01-01T00:00:00Z", level: "WARNING", message: "slow" }],
  );
  assert.equal(logLines("Job not found"), null);
});

void test("job rows read the summary fields and the total", () => {
  const out = jobRows({
    items: [
      {
        optimization_id: "id-1",
        name: "Run",
        status: "success",
        model_name: "openai/gpt",
        optimizer_name: "gepa",
        baseline_test_metric: 40,
        optimized_test_metric: 55,
      },
      { name: "no id" },
    ],
    total: 9,
  });
  assert.equal(out?.total, 9);
  assert.equal(out?.rows.length, 1);
  assert.equal(out?.rows[0].optimized, 55);
});

void test("stat rows scale a 0..1 success rate to percent", () => {
  assert.deepEqual(
    statRows({
      items: [{ name: "gepa", total_jobs: 4, success_rate: 0.75, avg_improvement: 3.2 }],
      truncated: true,
    }),
    { rows: [{ name: "gepa", runs: 4, successRate: 75, avgGain: 3.2 }], truncated: true },
  );
  assert.equal(statRows({ other: 1 }), null);
});

void test("status counts keep the non-zero statuses in a fixed order", () => {
  assert.deepEqual(statusCounts({ total: 5, success: 3, failed: 0, running: 2, shared: 1 }), {
    total: 5,
    counts: [
      ["running", 2],
      ["success", 3],
    ],
  });
  assert.equal(statusCounts({}), null);
});

void test("registry groups keep the kinds with names", () => {
  assert.deepEqual(registryGroups({ modules: ["a"], metrics: [], optimizers: ["gepa", ""] }), [
    ["modules", ["a"]],
    ["optimizers", ["gepa"]],
  ]);
  assert.equal(registryGroups({ x: 1 }), null);
});

void test("setting rows and flat pairs read the settings tools", () => {
  assert.deepEqual(
    settingRows({
      settings: [
        {
          key: "profile",
          tab: "account",
          summary: "Name",
          change_setting: true,
          protected_actions: ["a"],
        },
      ],
    }),
    [{ key: "profile", tab: "account", summary: "Name", changeable: true, protectedActions: 1 }],
  );
  assert.deepEqual(flatPairs({ name: "Dana", empty: null, flags: { a: 1 }, n: 2 }), [
    ["name", "Dana"],
    ["flags", '{"a":1}'],
    ["n", "2"],
  ]);
  assert.deepEqual(flatPairs("x"), []);
});

void test("an opened page reads a settings tab or a route", () => {
  assert.deepEqual(openedTarget({ navigate: { kind: "settings", tab: "billing" } }), {
    kind: "settings",
    target: "billing",
  });
  assert.deepEqual(openedTarget({ navigate: { kind: "route", path: "/explore" } }), {
    kind: "route",
    target: "/explore",
  });
  assert.equal(openedTarget({}), null);
});

void test("a tagging guide reads its name, question, categories and rule count", () => {
  assert.deepEqual(
    taggingGuide({
      session_id: "s1",
      name: "Tickets",
      question: "Is it urgent?",
      categories: ["yes", { name: "no" }, ""],
      rubric: [{}, {}],
    }),
    { name: "Tickets", question: "Is it urgent?", categories: ["yes", "no"], rules: 2 },
  );
  assert.equal(taggingGuide({ name: "x" }), null);
});

void test("a setup check reads its errors with their place", () => {
  assert.deepEqual(
    setupCheck({ ok: false, errors: [{ section: "run", key: "entry", message: "missing" }] }),
    {
      ok: false,
      errors: [{ where: "run.entry", message: "missing" }],
    },
  );
  assert.equal(setupCheck("x"), null);
});

void test("only http(s) links and same-site paths may be links", () => {
  assert.equal(linkHref("https://a.com/x"), "https://a.com/x");
  assert.equal(linkHref("HTTPS://A.COM/x"), "HTTPS://A.COM/x");
  assert.equal(linkHref("http://a.com"), "http://a.com");
  assert.equal(linkHref("/docs/runs"), "/docs/runs");
  assert.equal(linkHref("javascript:alert(1)"), "");
  assert.equal(linkHref("JavaScript:alert(1)"), "");
  assert.equal(linkHref("data:text/html,<b>x</b>"), "");
  assert.equal(linkHref("//evil.com/x"), "");
  assert.equal(linkHref("docs/runs"), "");
  assert.equal(linkHref(""), "");
  // A browser reads a backslash as a slash and drops a tab, so these go to evil.com.
  assert.equal(linkHref("/\\evil.com"), "");
  assert.equal(linkHref("/\t/evil.com"), "");
  assert.equal(linkHref("/\\evil.com", "https://app.example"), "");
  assert.equal(linkHref("/docs/runs", "https://app.example"), "/docs/runs");
});

const TAIL =
  "\n\n[Truncated: the result has 50000 characters and only the first 40000 are shown. Ask for a narrower path, file or query to see the rest.]";

void test("a GitHub list cut by cap_result reads as cut text, not as a failure", () => {
  assert.deepEqual(stripTruncated(`abc${TAIL}`), { text: "abc", cut: true });
  assert.deepEqual(stripTruncated("abc"), { text: "abc", cut: false });
  const whole = githubRows(
    "list_commits",
    `[{"sha":"abcdef1234","commit":{"message":"A"}}]${TAIL}`,
  );
  assert(whole?.kind === "rows");
  assert.equal(whole.cut, true);
  assert.equal(whole.rows[0]?.title, "A");
  const broken = githubRows("list_commits", `[{"sha":"abcdef1234","commit":{"mess${TAIL}`);
  assert.deepEqual(broken, {
    kind: "text",
    text: '[{"sha":"abcdef1234","commit":{"mess',
    cut: true,
  });
  // A cut folder list must not read as a file.
  assert.equal(githubFile(`[{"name":"a","type":"dir"},{"na${TAIL}`), null);
  assert(
    githubRows("get_file_contents", `[{"name":"a","type":"dir"},{"na${TAIL}`)?.kind === "text",
  );
});

void test("a GitHub item cut by cap_result reads as cut text, not as a failure", () => {
  const commit = {
    sha: "abcdef1234",
    commit: { message: "Big change" },
    files: Array.from({ length: 500 }, (_, i) => ({
      filename: `src/file_${i}.py`,
      status: "modified",
      patch: "x".repeat(200),
    })),
  };
  const whole = JSON.stringify(commit);
  assert(whole.length > 40_000);
  const tail = `\n\n[Truncated: the result has ${whole.length} characters and only the first 40000 are shown. Ask for a narrower path, file or query to see the rest.]`;
  const cut = githubItem("get_commit", whole.slice(0, 40_000) + tail);
  assert(cut?.kind === "text");
  assert.equal(cut.cut, true);
  assert.equal(cut.text, whole.slice(0, 40_000));
  const item = githubItem("get_commit", whole);
  assert(item?.kind === "item");
  assert.equal(item.title, "Big change");
  assert.equal(item.files.length, 500);
});

void test("a binary GitHub file reads as binary with no body", () => {
  assert.deepEqual(githubFile("successfully downloaded binary file (SHA: abc)"), {
    body: "",
    cut: false,
    binary: true,
  });
  assert.deepEqual(githubFile("successfully downloaded text file (SHA: abc)\n\n"), {
    body: "",
    cut: false,
    binary: false,
  });
});

void test("text that reads like a failure is content; only the error key is a failure", () => {
  assert.deepEqual(numberedFile("   1 Could not read it: 42 errors"), {
    kind: "file",
    lines: [{ n: 1, text: "Could not read it: 42 errors" }],
    cut: false,
  });
  assert.equal(repoTree("The folder is empty.")?.kind, "tree");
  assert.equal(repoTree("My notes.")?.kind, "tree");
  for (const error of [
    "Could not read it: 502 Bad Gateway",
    "Read limit for this turn reached. Work with what you have read, or ask the user.",
  ]) {
    assert.equal(repoTree({ error }), null, error);
    assert.equal(numberedFile({ error }), null, error);
  }
});

void test("an edit's new text found more than once in the file gets no line numbers", () => {
  assert.deepEqual(
    writtenLines("edit_workspace_file", { new_text: "x = 1" }, "x = 1\ny\nx = 1\n"),
    [{ n: null, text: "x = 1" }],
  );
});

void test("a GitHub file keeps a first line that is not the status line", () => {
  assert.deepEqual(githubFile("line1\n\nline3"), {
    body: "line1\n\nline3",
    cut: false,
    binary: false,
  });
  assert.deepEqual(githubFile("successfully downloaded text file (SHA: abc)\n\nline1\n\nline3"), {
    body: "line1\n\nline3",
    cut: false,
    binary: false,
  });
});

void test("a result the backend parsed into a number, a boolean or null reads as its text", () => {
  assert.notEqual(numberedFile(123), null);
  assert.deepEqual(repoTree(123), {
    kind: "tree",
    entries: [{ name: "123", depth: 0, dir: false, files: null, editable: false, more: null }],
    keyFiles: [],
    cut: false,
  });
  assert.deepEqual(githubFile(123), { body: "123", cut: false, binary: false });
  assert.deepEqual(githubFile(true), { body: "true", cut: false, binary: false });
  assert.deepEqual(githubFile(null), { body: "null", cut: false, binary: false });
  assert.deepEqual(githubRows("list_commits", false), { kind: "text", text: "false", cut: false });
  assert.deepEqual(workspaceSave(true), { note: "true" });
  assert.equal(githubFile(undefined), null);
});

void test("only the exact tree header starts the root overview", () => {
  // A root file named like the header is an entry, not the header.
  assert.deepEqual(repoTree("Tree (x)\nsrc/").kind === "tree" && repoTree("Tree (x)\nsrc/"), {
    kind: "tree",
    entries: [
      { name: "Tree (x)", depth: 0, dir: false, files: null, editable: false, more: null },
      { name: "src", depth: 0, dir: true, files: null, editable: false, more: null },
    ],
    keyFiles: [],
    cut: false,
  });
});

void test("open_repo's reasons arrive as errors, not as a file or a tree", () => {
  for (const reason of [
    "No repository is chosen yet.",
    "GitHub is not linked, so the repository cannot be read.",
    "GitLab refused to list acme/app.",
  ]) {
    assert.equal(repoTree({ error: reason }), null, reason);
    assert.equal(numberedFile({ error: reason }), null, reason);
  }
});

void test("an edit's lines take their numbers from the saved file, a write shows the file", () => {
  const saved = "a\n  b\n  c\nd\n";
  assert.deepEqual(writtenLines("edit_workspace_file", { new_text: "  b\n  c" }, saved), [
    { n: 2, text: "  b" },
    { n: 3, text: "  c" },
  ]);
  assert.deepEqual(writtenLines("edit_workspace_file", { new_text: "zz" }, saved), [
    { n: null, text: "zz" },
  ]);
  assert.deepEqual(
    writtenLines("write_workspace_file", { text: "ignored" }, " x\ny").map((l) => [l.n, l.text]),
    [
      [1, " x"],
      [2, "y"],
    ],
  );
});

void test("every parser returns its empty value for garbage input and does not throw", () => {
  const garbage: unknown[] = [
    null,
    undefined,
    0,
    42,
    true,
    [],
    [1, "a", null],
    {},
    { x: 1 },
    "",
    "}{",
    "[",
    "null",
    "Could not",
  ];
  for (const g of garbage) {
    assert.doesNotThrow(() => {
      excerpt(typeof g === "string" ? g : "");
      hostOf(typeof g === "string" ? g : "");
      linkHref(typeof g === "string" ? g : "");
      plainLines(typeof g === "string" ? g : "");
      writeCounts(
        "write_workspace_file",
        (g && typeof g === "object" ? g : {}) as Record<string, unknown>,
      );
    });
    const webL = webLinks(g);
    assert(webL === null || Array.isArray(webL));
    const pages = webPages(g);
    assert(pages === null || pages.pages.every((p) => p.url || p.title || p.excerpt));
    // Text, and a scalar the backend parsed from text, read; a list or an object does not.
    const text =
      typeof g === "string" || typeof g === "number" || typeof g === "boolean" || g === null;
    for (const read of [numberedFile, repoTree]) {
      const v = read(g);
      if (g == null) assert.deepEqual(v, { kind: "none" });
      else assert(text ? v !== null : v === null);
    }
    const ws = workspaceSave(g);
    assert(ws === null || "note" in ws);
    for (const tool of [
      "search_repositories",
      "search_code",
      "list_commits",
      "list_branches",
      "list_tags",
      "list_releases",
      "get_file_contents",
    ]) {
      const rows = githubRows(tool, g);
      assert(rows === null || rows.kind === "text" || rows.rows.length === 0);
    }
    for (const tool of ["get_commit", "get_tag", "get_latest_release", "get_release_by_tag"]) {
      const item = githubItem(tool, g);
      assert(item === null || item.kind === "text");
    }
    const file = githubFile(g);
    assert(text || file === null);
    const logs = logLines(g);
    assert(logs === null || logs.length === 0);
    const jobs = jobRows(g);
    assert(jobs === null || jobs.rows.length === 0);
    const stats = statRows(g);
    assert(stats === null);
    assert.equal(statusCounts(g), null);
    assert.equal(registryGroups(g), null);
    assert.equal(settingRows(g), null);
    assert.deepEqual(
      flatPairs(g),
      g && typeof g === "object" && !Array.isArray(g) && "x" in g ? [["x", "1"]] : [],
    );
    assert.equal(openedTarget(g), null);
    assert.equal(taggingGuide(g), null);
    assert.equal(setupCheck(g), null);
  }
});
