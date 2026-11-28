/*
 * node --test cannot load .tsx, so this runs through jiti (tool-result-cards.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { AgentToolCall } from "@/shared/ui/agent/types";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const {
  CreatedRepoCard,
  GithubToolCard,
  JobListCard,
  JobLogsCard,
  LinkResultsCard,
  OpenPageCard,
  RegistryCard,
  RepoFileCard,
  RepoFolderCard,
  RunLifecycleCard,
  SettingValuesCard,
  SetupCheckCard,
  ToolCardBoundary,
  UsageStatsCard,
  WebPagesCard,
  WorkspaceWriteCard,
} = await import("./ToolResultCards");
const { ToolCallRow } = await import("./ToolCallRow");
const { TOOL_ICONS, getToolMeta } = await import("../lib/tool-meta");
const { formatTemplate } = await import("@/shared/lib/i18n");
const { ascentTomlDiagnosticLines } = await import("@/shared/lib/api");
const localeDir = new URL("../../../../../i18n/locales/ui/", import.meta.url);
function catalog(locale: string): Record<string, string> {
  return createRequire(import.meta.url)(new URL(`${locale}.json`, localeDir).pathname) as Record<
    string,
    string
  >;
}

function call(
  tool: string,
  result: unknown,
  args: Record<string, unknown> = {},
  status: AgentToolCall["status"] = "done",
): AgentToolCall {
  return {
    id: "c1",
    tool,
    reason: "",
    status,
    startedAt: 0,
    endedAt: 1,
    payload: { arguments: args, result },
  };
}

/** Render with every StepRow open: its open state starts false, so start it true. */
function renderOpen(el: React.ReactElement): string {
  // The module namespace is read-only; the CommonJS exports behind it are not.
  const react = createRequire(import.meta.url)("react") as { useState: typeof React.useState };
  const real = react.useState;
  react.useState = ((init: unknown) => real(init === false ? true : init)) as typeof React.useState;
  try {
    return renderToStaticMarkup(el);
  } finally {
    react.useState = real;
  }
}

/** The failed mark: the step's icon in the danger tone. */
const FAILED = /class="size-3\.5 shrink-0 text-\[var\(--danger\)\]"/;

/** Render with a request catalog, so count and "more" labels show their numbers. */
function withMessages(messages: Record<string, string>, render: () => string): string {
  // msg() reads the request catalog when there is no window.
  const before = globalThis.__APP_REQUEST_MESSAGES__;
  globalThis.__APP_REQUEST_MESSAGES__ = messages;
  try {
    return render();
  } finally {
    globalThis.__APP_REQUEST_MESSAGES__ = before;
  }
}

/** Only the English ``ascent.toml`` mistake lines, so other labels still render as keys. */
function setupCheckMessages(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(catalog("en")).filter(([key]) => key.startsWith("submit.setup_check.")),
  );
}

void describe("tool result cards", () => {
  void it("links only http(s) URLs and site paths", () => {
    const html = renderOpen(
      <LinkResultsCard
        call={call("web_search", [
          { title: "Safe", url: "HTTPS://a.com/x" },
          { title: "Script", url: "javascript:alert(1)" },
          { title: "Data", url: "data:text/html,x" },
          { title: "Proto", url: "//evil.com/x" },
          { title: "Docs", url: "/docs/runs" },
        ])}
      />,
    );
    assert.match(html, /href="HTTPS:\/\/a.com\/x" target="_blank" rel="noopener noreferrer"/);
    assert.match(html, /href="\/docs\/runs"(?! target)/);
    assert.doesNotMatch(html, /href="(javascript:|data:|\/\/evil)/);
    assert.equal(html.match(/<a /g)?.length, 2);
  });

  void it("an empty stats result does not open to an empty panel", () => {
    const html = renderToStaticMarkup(
      <UsageStatsCard call={call("get_model_stats", { items: [] })} />,
    );
    assert.doesNotMatch(html, /aria-expanded/);
    const full = renderToStaticMarkup(
      <UsageStatsCard
        call={call("get_model_stats", { items: [{ name: "gpt", total_jobs: 2 }] })}
      />,
    );
    assert.match(full, /aria-expanded/);
  });

  void it("a setting change never shows the values the call sent", () => {
    const html = renderOpen(
      <SettingValuesCard
        call={call(
          "change_setting",
          { ok: true },
          { key: "keys", values: { api_key: "sk-secret-123" } },
        )}
      />,
    );
    assert.doesNotMatch(html, /sk-secret-123/);
    const masked = renderOpen(
      <SettingValuesCard
        call={call("change_setting", { ok: true, result: { api_key: "sk-…123" } }, { key: "keys" })}
      />,
    );
    assert.match(masked, /sk-…123/);
  });

  void it("a crawl page with no URL still shows", () => {
    const html = renderOpen(
      <WebPagesCard
        call={call("crawl_site", { pages: [{ title: "No link page" }, { title: "Second" }] })}
      />,
    );
    assert.match(html, /No link page/);
    assert.match(html, /Second/);
  });

  void it("a binary GitHub file shows a note, not a failure", () => {
    const html = renderOpen(
      <GithubToolCard
        call={call("get_file_contents", "successfully downloaded binary file (SHA: abc)", {
          path: "logo.png",
        })}
      />,
    );
    assert.doesNotMatch(html, /data-status="error"|text-\[var\(--danger\)\]/);
    assert.match(html, /logo\.png/);
    assert.match(html, /agent\.tool\.file\.binary/);
  });

  void it("a GitHub commit cut by cap_result shows its text as cut, not as a failure", () => {
    const whole = JSON.stringify({
      sha: "abcdef1234",
      commit: { message: "Big change" },
      files: Array.from({ length: 500 }, (_, i) => ({
        filename: `f${i}.py`,
        patch: "x".repeat(200),
      })),
    });
    const tail = `\n\n[Truncated: the result has ${whole.length} characters and only the first 40000 are shown. Ask for a narrower path, file or query to see the rest.]`;
    const html = renderOpen(
      <GithubToolCard
        call={call("get_commit", whole.slice(0, 40_000) + tail, {
          owner: "o",
          repo: "r",
          sha: "abcdef1",
        })}
      />,
    );
    assert.doesNotMatch(html, /data-status="error"|text-\[var\(--danger\)\]/);
    assert.match(html, /agent\.tool\.content\.cut/);
    assert.match(html, /abcdef1234/);
  });

  void it("a repo read flagged as an error fails with its text; an empty folder does not", () => {
    const error = "No file named 'a.py' in the repository.";
    const file = renderOpen(
      <RepoFileCard call={call("read_repo_file", { error }, { path: "a.py" })} />,
    );
    assert.match(file, /text-\[var\(--danger\)\]/);
    // A screen reader hears the failure, not only a red icon.
    assert.match(file, /role="img" aria-label="agent_run\.transcript\.tool_failed"/);
    assert.match(file, /No file named &#x27;a\.py&#x27; in the repository\./);
    const missing = renderOpen(
      <RepoFolderCard call={call("list_repo_folder", { error: "No folder named 'x'." })} />,
    );
    assert.match(missing, /text-\[var\(--danger\)\]/);
    const empty = renderOpen(
      <RepoFolderCard
        call={call("list_repo_folder", { note: "The folder is empty." }, { path: "e" })}
      />,
    );
    assert.doesNotMatch(empty, /text-\[var\(--danger\)\]/);
    assert.match(empty, /agent\.tool\.folder\.empty/);
  });

  void it("an old saved repo read with a null result shows a plain done step", () => {
    const file = renderOpen(<RepoFileCard call={call("read_repo_file", null, { path: "a.py" })} />);
    const folder = renderOpen(
      <RepoFolderCard call={call("list_repo_folder", null, { path: "src" })} />,
    );
    for (const html of [file, folder]) {
      assert.doesNotMatch(html, /text-\[var\(--danger\)\]/);
      assert.doesNotMatch(html, />null</);
      assert.doesNotMatch(html, /agent\.tool\.(file|folder)\.(lines|items|empty)/);
    }
    assert.match(file, /a\.py/);
    assert.match(folder, /src/);
  });

  void it("an old saved folder read with its path only in reason shows that path", () => {
    const old = { ...call("list_repo_folder", null), reason: "src/app" };
    const html = renderToStaticMarkup(<RepoFolderCard call={old} />);
    assert.match(html, /src\/app/);
  });

  void it("a cut file says so in the closed step line, for a repo read and a GitHub read", () => {
    const body = Array.from({ length: 3 }, (_, i) => `${String(i + 1).padStart(4)} x`).join("\n");
    const repo = renderToStaticMarkup(
      <RepoFileCard
        call={call("read_repo_file", `${body}\n... (file cut here)`, { path: "a.py" })}
      />,
    );
    const github = renderToStaticMarkup(
      <GithubToolCard
        call={call("get_file_contents", "a\nb\n\n[Truncated: 9 more lines]", { path: "a.py" })}
      />,
    );
    for (const html of [repo, github]) assert.match(html, /agent\.tool\.content\.cut/);
    const whole = renderToStaticMarkup(
      <RepoFileCard call={call("read_repo_file", body, { path: "a.py" })} />,
    );
    assert.doesNotMatch(whole, /agent\.tool\.content\.cut/);
  });

  void it("the folder count counts only the folder's own items, not nested ones", () => {
    const tree = ["a/", "  1.py", "  2.py", "  3.py", "b/", "  4.py", "c.py"].join("\n");
    // msg() reads the request catalog when there is no window.
    const before = globalThis.__APP_REQUEST_MESSAGES__;
    globalThis.__APP_REQUEST_MESSAGES__ = { "agent.tool.folder.items": "{count} items" };
    try {
      const html = renderToStaticMarkup(
        <RepoFolderCard call={call("list_repo_folder", tree, { path: "src" })} />,
      );
      assert.match(html, />3 items</);
    } finally {
      globalThis.__APP_REQUEST_MESSAGES__ = before;
    }
  });

  void it("a write the tool refuses shows its refusal as a failure", () => {
    const error = "Not a file you can write: 'run.py'. Write one of metric.py, ascent.toml.";
    const html = renderOpen(
      <WorkspaceWriteCard call={call("write_workspace_file", { error }, { path: "run.py" })} />,
    );
    assert.match(html, /text-\[var\(--danger\)\]/);
    assert.match(html, /Not a file you can write/);
  });

  void it("a saved ascent.toml with parser errors shows each error, yet the write succeeds", () => {
    const text = "[run]\nname = 1\n";
    const result = {
      wizard_state: { ascent_toml: text },
      ascent_toml_diagnostics: [
        { level: "error", line: 1, section: "run", key: "entry", message: "entry is missing" },
        { level: "error", line: 2, section: "run", key: "name", message: "name is wrong" },
      ],
    };
    const html = withMessages(setupCheckMessages(), () =>
      renderOpen(
        <WorkspaceWriteCard
          call={call("write_workspace_file", result, { path: "ascent.toml", text })}
        />,
      ),
    );
    assert.match(
      html,
      /\[run\.entry\]<\/code><span dir="auto" class="text-\[var\(--danger\)\]">Error at line 1: entry is missing<\/span>/,
    );
    assert.match(html, /\[run\.name\]<\/code><span[^>]*>Error at line 2: name is wrong<\/span>/);
    assert.match(html, /agent\.tool\.check_setup\.failed/);
    assert.match(html, /text-\[var\(--success\)\]">\+2</);
    assert.doesNotMatch(html, /aria-label="agent\.tool\.write_workspace_file\.title"/);
    assert.doesNotMatch(html, /line-clamp-4/);
  });

  void it("the count labels take the singular for one and the right form for many", () => {
    const en = catalog("en");
    const count = (
      template: string | undefined,
      n: number,
      locale: "en" | "ru" | "ar" | "he" = "en",
    ) => formatTemplate(template ?? "", { count: n }, locale);
    assert.equal(count(en["agent.tool.file.lines"], 1), "1 line");
    assert.equal(count(en["agent.tool.file.lines"], 2), "2 lines");
    assert.equal(count(en["agent.tool.folder.files"], 1), "1 file");
    assert.equal(count(en["agent.tool.tagging_guide.categories"], 1), "1 category");
    assert.equal(count(en["agent.tool.runs.count"], 1), "1 optimization");
    assert.equal(count(en["agent.tool.runs.count"], 3), "3 optimizations");
    const ru = catalog("ru");
    assert.equal(count(ru["agent.tool.file.lines"], 21, "ru"), "21 строка");
    assert.equal(count(ru["agent.tool.file.lines"], 3, "ru"), "3 строки");
    assert.equal(count(ru["agent.tool.file.lines"], 5, "ru"), "5 строк");
    const ar = catalog("ar")["agent.tool.runs.count"];
    assert.equal(count(ar, 0, "ar"), "لا عمليات تشغيل");
    assert.equal(count(ar, 1, "ar"), "تشغيل واحد");
    assert.equal(count(ar, 2, "ar"), "تشغيلان");
    // Arabic wraps the number in direction isolates.
    assert.equal(count(ar, 3, "ar"), "\u20683\u2069 عمليات تشغيل");
    // Hebrew literals must stay in the catalogs, so "one" (feminine) is escaped here.
    assert.equal(
      count(catalog("he")["agent.tool.runs.count"], 1, "he"),
      `${formatTemplate("{term.optimizationTypeRun}", undefined, "he")} \u05d0\u05d7\u05ea`,
    );
  });

  void it("a read tool's running row keeps the tool's own icon", () => {
    assert.ok(TOOL_ICONS.read_repo_file);
    assert.equal(getToolMeta("read_repo_file").icon, TOOL_ICONS.read_repo_file);
    assert.notEqual(getToolMeta("read_repo_file").icon, getToolMeta("no_such_tool").icon);
  });

  void it("a card that throws shows the fallback line", () => {
    const c = call("web_search", []);
    const boundary = new ToolCardBoundary({
      call: c,
      card: () => {
        throw new Error("bad");
      },
    });
    boundary.state = ToolCardBoundary.getDerivedStateFromError();
    const html = renderToStaticMarkup(<>{boundary.render()}</>);
    assert.doesNotMatch(html, /bad/);
    assert.ok(html.length > 0);
  });

  void it("a failed card tries again when its call or its result changes", () => {
    const first = call("web_search", []);
    const make = (c: AgentToolCall) => {
      const b = new ToolCardBoundary({ call: c, card: () => null });
      b.state = ToolCardBoundary.getDerivedStateFromError();
      const states: Array<{ failed: boolean }> = [];
      b.setState = ((s: { failed: boolean }) => states.push(s)) as typeof b.setState;
      return { b, states };
    };
    const same = make(first);
    same.b.componentDidUpdate({ call: first });
    assert.deepEqual(same.states, []);
    const next = make({ ...first, payload: { ...first.payload, result: [{ title: "A" }] } });
    next.b.componentDidUpdate({ call: first });
    assert.deepEqual(next.states, [{ failed: false }]);
    const other = make({ ...first, id: "c2" });
    other.b.componentDidUpdate({ call: first });
    assert.deepEqual(other.states, [{ failed: false }]);
  });

  void it("a card that throws logs the error", () => {
    const b = new ToolCardBoundary({ call: call("web_search", []), card: () => null });
    const real = console.error;
    const logged: unknown[][] = [];
    console.error = (...args: unknown[]) => void logged.push(args);
    try {
      b.componentDidCatch(new Error("bad"));
    } finally {
      console.error = real;
    }
    assert.equal(logged.length, 1);
    assert.match(String(logged[0]?.[0]), /web_search/);
  });

  void it("an external link says it opens a new tab; a site path does not", () => {
    const html = renderOpen(
      <LinkResultsCard
        call={call("web_search", [
          { title: "Out", url: "https://a.com/x" },
          { title: "In", url: "/docs/runs" },
        ])}
      />,
    );
    assert.equal(html.match(/<span class="sr-only"> agent\.tool\.new_tab<\/span>/g)?.length, 1);
    assert.match(html, /Out<\/span><span class="sr-only">/);
  });

  void it("a crawl that used up its budget says so, apart from a partial crawl", () => {
    const html = renderToStaticMarkup(
      <WebPagesCard
        call={call("crawl_site", { status: "completed", truncated: true, pages: [{ title: "A" }] })}
      />,
    );
    assert.match(html, /agent\.tool\.pages\.budget/);
    assert.doesNotMatch(html, /agent\.tool\.pages\.partial/);
  });

  void it("a commit with no line changes shows no empty counts", () => {
    const commit = (additions: number, deletions: number) =>
      renderToStaticMarkup(
        <GithubToolCard
          call={call(
            "get_commit",
            { sha: "abcdef1234", commit: { message: "Msg" }, stats: { additions, deletions } },
            { owner: "o", repo: "r", sha: "abcdef1" },
          )}
        />,
      );
    assert.doesNotMatch(commit(0, 0), /tabular-nums/);
    assert.match(commit(2, 0), /\+2/);
  });

  void it("the stats name column and the lock and shield icons have names", () => {
    const stats = renderOpen(
      <UsageStatsCard
        call={call("get_model_stats", { items: [{ name: "gpt", total_jobs: 2 }] })}
      />,
    );
    assert.match(stats, /<th[^>]*><span class="sr-only">agent\.tool\.stats\.name<\/span><\/th>/);
    const repos = renderOpen(
      <GithubToolCard
        call={call("search_repositories", { items: [{ full_name: "o/r", private: true }] })}
      />,
    );
    assert.match(
      repos,
      /<svg[^>]*role="img"[^>]*aria-label="agent\.tool\.github_repos\.private"|<svg[^>]*aria-label="agent\.tool\.github_repos\.private"[^>]*role="img"/,
    );
  });

  void it("a toast lists ten diagnostics, errors first, then counts the rest (SKY-763)", () => {
    const items = [
      { level: "warning" as const, line: 1, message: "old style" },
      ...Array.from({ length: 11 }, (_, i) => ({
        level: "error" as const,
        line: i + 2,
        message: `bad ${i}`,
      })),
    ];
    const text = withMessages(setupCheckMessages(), () => ascentTomlDiagnosticLines(items));
    const lines = text.split("\n");
    assert.equal(lines[0], "");
    assert.equal(lines.length, 12);
    assert.equal(lines[1], "Error at line 2: bad 0");
    assert.equal(lines[10], "Error at line 11: bad 9");
    assert.equal(lines[11], "And 2 more.");
    assert.equal(
      withMessages(setupCheckMessages(), () => ascentTomlDiagnosticLines(items.slice(0, 10))),
      [
        "",
        "Error at line 2: bad 0",
        ...items.slice(2, 10).map((d) => `Error at line ${d.line}: ${d.message}`),
        "Warning at line 1: old style",
      ].join("\n"),
    );
    assert.equal(ascentTomlDiagnosticLines([]), "");
  });

  void it("a toast adds the errors past the parser cap to the count (SKY-763)", () => {
    const items = Array.from({ length: 12 }, (_, i) => ({
      level: "error" as const,
      line: i + 1,
      message: `bad ${i}`,
    }));
    const many = withMessages(setupCheckMessages(), () => ascentTomlDiagnosticLines(items, 100));
    assert.equal(many.split("\n").at(-1), "And 102 more.");
    const few = withMessages(setupCheckMessages(), () =>
      ascentTomlDiagnosticLines(items.slice(0, 3), 5),
    );
    assert.deepEqual(few.split("\n").slice(1), [
      "Error at line 1: bad 0",
      "Error at line 2: bad 1",
      "Error at line 3: bad 2",
      "And 5 more.",
    ]);
  });

  void it("the save and setup check cards count the diagnostics the result leaves out (SKY-763)", () => {
    const error = {
      level: "error",
      line: 1,
      section: "run",
      key: "entry",
      message: "entry is missing",
    };
    const text = "[run]\n";
    const saved = withMessages(setupCheckMessages(), () =>
      renderOpen(
        <WorkspaceWriteCard
          call={call(
            "write_workspace_file",
            {
              wizard_state: { ascent_toml: text },
              ascent_toml_diagnostics: [error],
              ascent_toml_diagnostics_omitted: 580,
            },
            { path: "ascent.toml", text },
          )}
        />,
      ),
    );
    assert.match(saved, />And 580 more\.<\/li>/);
    const checked = withMessages(setupCheckMessages(), () =>
      renderOpen(
        <SetupCheckCard
          call={call("check_setup", { ok: false, errors: [error], errors_omitted: 19 })}
        />,
      ),
    );
    assert.match(checked, />And 19 more\.<\/li>/);
    const whole = withMessages(setupCheckMessages(), () =>
      renderOpen(<SetupCheckCard call={call("check_setup", { ok: false, errors: [error] })} />),
    );
    assert.doesNotMatch(whole, /more\.<\/li>/);
  });

  void it("a setup check says ok or failed, and puts [section.key] before each error and warning", () => {
    const ok = renderOpen(<SetupCheckCard call={call("check_setup", { ok: true, errors: [] })} />);
    assert.match(ok, /<span>agent\.tool\.check_setup\.ok<\/span>/);
    assert.doesNotMatch(ok, /agent\.tool\.check_setup\.failed/);
    const bad = withMessages(setupCheckMessages(), () =>
      renderOpen(
        <SetupCheckCard
          call={call("check_setup", {
            ok: false,
            errors: [
              {
                level: "error",
                line: 4,
                section: "run",
                key: "entry",
                message: "entry is missing",
              },
              { level: "error", line: null, section: "metric", message: "metric is wrong" },
              { level: "warning", line: null, message: "no place" },
            ],
          })}
        />,
      ),
    );
    assert.match(
      bad,
      /text-\[var\(--danger\)\]"><svg[^>]*>.*?<\/svg>agent\.tool\.check_setup\.failed/,
    );
    assert.doesNotMatch(bad, /agent\.tool\.check_setup\.ok/);
    assert.match(
      bad,
      /<code[^>]*>\[run\.entry\]<\/code><span[^>]*>Error at line 4: entry is missing<\/span>/,
    );
    assert.match(bad, /<code[^>]*>\[metric\]<\/code><span[^>]*>Error: metric is wrong<\/span>/);
    assert.match(
      bad,
      /<li class="min-w-0 break-words"><span dir="auto" class="text-\[var\(--warning\)\]">Warning: no place<\/span>/,
    );
  });

  void it("a run lifecycle call with no status fails; the run id shows 8 characters", () => {
    const args = { optimization_id: "abcdefghijkl" };
    const none = renderOpen(<RunLifecycleCard call={call("pause_job", {}, args)} />);
    assert.match(none, FAILED);
    const paused = renderOpen(
      <RunLifecycleCard call={call("pause_job", { status: "paused" }, args)} />,
    );
    assert.doesNotMatch(paused, FAILED);
    assert.match(paused, />abcdefgh</);
    assert.doesNotMatch(paused, /abcdefghi/);
  });

  void it("a created repository with no name fails; the commit shows 7 characters", () => {
    const none = renderOpen(<CreatedRepoCard call={call("create_github_repository", {})} />);
    assert.match(none, FAILED);
    const made = renderOpen(
      <CreatedRepoCard
        call={call("create_github_repository", {
          full_name: "o/r",
          default_branch: "main",
          commit: "0123456789abc",
        })}
      />,
    );
    assert.doesNotMatch(made, FAILED);
    assert.match(made, /href="https:\/\/github\.com\/o\/r"/);
    assert.match(made, /<code[^>]*>0123456<\/code>/);
  });

  void it("a run list shows shown of total only when more runs match; a name falls back to the id", () => {
    const messages = {
      "agent.tool.shown_of_total": "{shown} of {total}",
      "agent.tool.runs.count": "{count} runs",
    };
    const rows = [{ optimization_id: "0123456789ab", status: "success" }];
    const more = withMessages(messages, () =>
      renderOpen(<JobListCard call={call("list_jobs", { items: rows, total: 5 })} />),
    );
    assert.match(more, />1 of 5</);
    assert.match(more, /<span dir="auto">01234567<\/span>/);
    const all = withMessages(messages, () =>
      renderOpen(
        <JobListCard
          call={call("list_jobs", { items: [{ ...rows[0], name: "My run" }], total: 1 })}
        />,
      ),
    );
    assert.match(all, />1 runs</);
    assert.doesNotMatch(all, / of /);
    assert.match(all, /<span dir="auto">My run<\/span>/);
  });

  void it("a run list shows a better minimize run as a gain (SKY-750)", () => {
    const rows = [
      {
        optimization_id: "0123456789ab",
        baseline_test_metric: 10,
        optimized_test_metric: 5,
        metric_improvement: 5,
      },
    ];
    const html = renderOpen(<JobListCard call={call("list_jobs", { items: rows, total: 1 })} />);
    assert.match(html, /\+5\.0%/);
    assert.doesNotMatch(html, /-5\.0%/);
  });

  void it("job logs show only the newest 400 lines but count all of them", () => {
    const items = Array.from({ length: 405 }, (_, i) => ({ level: "info", message: `line-${i}` }));
    const html = withMessages({ "agent.tool.logs.count": "{count} lines" }, () =>
      renderOpen(<JobLogsCard call={call("get_job_logs", { items })} />),
    );
    assert.match(html, />405 lines</);
    assert.doesNotMatch(html, />line-4</);
    assert.match(html, />line-5</);
    assert.match(html, />line-404</);
  });

  void it("an opened page falls back to the call's tab or path argument", () => {
    const tab = renderOpen(<OpenPageCard call={call("open_page", {}, { tab: "keys" })} />);
    assert.match(tab, /<span class="min-w-0 truncate">keys<\/span>/);
    const path = renderOpen(<OpenPageCard call={call("open_page", {}, { path: "/runs" })} />);
    assert.match(path, /<span class="min-w-0 truncate">\/runs<\/span>/);
    const opened = renderOpen(
      <OpenPageCard
        call={call("open_page", { navigate: { kind: "route", path: "/docs" } }, { tab: "keys" })}
      />,
    );
    assert.match(opened, /<span class="min-w-0 truncate">\/docs<\/span>/);
    assert.doesNotMatch(opened, />keys</);
  });

  void it("a registry kind with more than 24 names shows 24 and a more row", () => {
    const modules = Array.from({ length: 26 }, (_, i) => `mod${i}`);
    const html = withMessages(
      { "auto.features.agent.panel.components.resultcards.more": "+{p1} more" },
      () => renderOpen(<RegistryCard call={call("get_registry_snapshot", { modules })} />),
    );
    assert.equal(html.match(/<code /g)?.length, 24);
    assert.match(html, />\+2 more</);
    assert.doesNotMatch(html, />mod24</);
  });
  void it("a running command shows its live output, then only the final result (SKY-1006)", () => {
    const running: AgentToolCall = {
      ...call("exec_command", undefined, { cmd: "make" }, "running"),
      payload: { arguments: { cmd: "make" }, liveOutput: "building step 1\nbuilding step 2" },
    };
    const live = renderOpen(<ToolCallRow call={running} />);
    assert.match(live, /data-testid="tool-live-output"/);
    assert.match(live, /building step 1\nbuilding step 2/);

    const done = renderOpen(
      <ToolCallRow call={call("exec_command", "Output:\nall done", { cmd: "make" })} />,
    );
    assert.doesNotMatch(done, /tool-live-output/);
    assert.match(done, /Output:\nall done/);

    // A stale live text on a finished call never shows.
    const stale: AgentToolCall = {
      ...call("exec_command", "final", {}),
      payload: { result: "final", liveOutput: "old" },
    };
    assert.doesNotMatch(renderOpen(<ToolCallRow call={stale} />), /old/);
  });
});
