import assert from "node:assert/strict";
import test from "node:test";
import { extractHeadings, remarkDocs, searchText, slugify, tabLabel } from "./markdown.ts";

test("slugify keeps Hebrew letters and drops punctuation", () => {
  assert.equal(slugify("Tools & scopes"), "tools-scopes");
  assert.equal(slugify("כלים והרשאות"), "כלים-והרשאות");
});

test("slugify keeps combining marks, so Devanagari words stay whole", () => {
  assert.equal(slugify("कनेक्शन स्वीकृत करें"), "कनेक्शन-स्वीकृत-करें");
});

test("extractHeadings skips fenced code and gives repeated headings unique ids", () => {
  const md = "## Setup\n\n```bash\n## not a heading\n```\n\n### `run_id`\n\n## Setup\n";
  assert.deepEqual(extractHeadings(md), [
    { depth: 2, text: "Setup", id: "setup" },
    { depth: 3, text: "run_id", id: "run-id" },
    { depth: 2, text: "Setup", id: "setup-2" },
  ]);
});

test("searchText drops code, links and callout markers", () => {
  const md = "> [!NOTE]\n> Read [the API](/docs/api).\n\n```bash\nsecret-looking-code\n```\n";
  assert.equal(searchText(md), "Read the API.");
});

test("tabLabel reads the tab name from a code block info string", () => {
  assert.equal(tabLabel('tab="macOS"'), "macOS");
  assert.equal(tabLabel("title=x"), null);
  assert.equal(tabLabel(null), null);
});

test("remarkDocs groups adjacent tab code blocks once, unwraps callouts and marks headings", () => {
  const code = (label: string) => ({
    type: "code",
    lang: "bash",
    meta: `tab="${label}"`,
    value: label,
  });
  const tree = {
    type: "root",
    children: [
      { type: "heading", depth: 2, children: [{ type: "text", value: "Install" }] },
      code("A"),
      code("B"),
      {
        type: "blockquote",
        children: [{ type: "paragraph", children: [{ type: "text", value: "[!TIP]\nUse it." }] }],
      },
      {
        type: "blockquote",
        children: [
          { type: "paragraph", children: [{ type: "text", value: "[!WARNING]" }] },
          {
            type: "blockquote",
            children: [
              { type: "paragraph", children: [{ type: "text", value: "[!NOTE]\nSecond." }] },
            ],
          },
        ],
      },
      {
        type: "blockquote",
        children: [{ type: "paragraph", children: [{ type: "text", value: "A quote." }] }],
      },
      { type: "code", lang: "skynet-mcp-clients", value: "" },
    ],
  };
  remarkDocs({ headingIds: ["install"] })(tree);
  const [heading, tabs, tip, warning, quote, mcp] = tree.children as Array<Record<string, any>>;
  assert.equal(heading.data.hProperties.id, "install");
  assert.equal(tabs.type, "docsTabs");
  assert.equal(tabs.data.hProperties.dataTabs, '["A","B"]');
  assert.deepEqual(
    tabs.children.map((c: { type: string }) => c.type),
    ["code", "code"],
  );
  assert.equal(tip.type, "paragraph");
  assert.equal(tip.children[0].value, "Use it.");
  assert.equal(warning.type, "paragraph");
  assert.equal(warning.children[0].value, "Second.");
  assert.equal(quote.type, "blockquote");
  assert.equal(quote.data, undefined);
  assert.equal(mcp.type, "docsMcpClients");
  assert.equal(mcp.value, undefined);
});
