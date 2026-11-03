/**
 * Pure Markdown helpers for the public docs: heading ids, the table of
 * contents, search text, and one remark plugin for the docs-only syntax.
 *
 * Docs-only syntax, so plain Markdown readers still see sensible text:
 * - `> [!NOTE]`, `> [!TIP]`, `> [!WARNING]` (GitHub style) mark text that the
 *   docs show as plain paragraphs, with no box, icon or label.
 * - Adjacent fenced code blocks with `tab="Label"` in the info string show as
 *   one block with tabs.
 * - A fenced block in the `skynet-mcp-clients` language marks where the MCP
 *   client setup tabs go.
 *
 * Kept free of `@/` imports so `node --test` can load it.
 */

export interface DocsHeading {
  depth: 2 | 3;
  text: string;
  id: string;
}

export const MCP_CLIENTS_BLOCK = "skynet-mcp-clients";

const CALLOUT_KINDS = ["note", "tip", "warning"];

/** Remove inline Markdown (code ticks, links, emphasis) from a line of text. */
export function stripInline(text: string): string {
  return text
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/(^|[^\w*])[*_]([^*_]+)[*_](?=[^\w*]|$)/g, "$1$2")
    .trim();
}

/**
 * A URL fragment for a heading. Keeps letters, combining marks and digits in
 * any script, so Hebrew and Devanagari headings get readable ids.
 */
export function slugify(text: string): string {
  return stripInline(text)
    .toLowerCase()
    .replace(/[^\p{L}\p{M}\p{N}]+/gu, "-")
    .replace(/^-+|-+$/g, "");
}

/** Lines of Markdown outside fenced code blocks, with their fence state. */
function* proseLines(markdown: string): Generator<string> {
  let fence: string | null = null;
  for (const line of markdown.split("\n")) {
    const marker = line.match(/^\s*(`{3,}|~{3,})/)?.[1];
    if (marker) {
      if (fence === null) fence = marker;
      else if (marker.startsWith(fence)) fence = null;
      continue;
    }
    if (fence === null) yield line;
  }
}

/**
 * The level-2 and level-3 headings of a page, in order, with unique ids.
 * The remark plugin gives the rendered headings these same ids.
 */
export function extractHeadings(markdown: string): DocsHeading[] {
  const seen = new Map<string, number>();
  const headings: DocsHeading[] = [];
  for (const line of proseLines(markdown)) {
    const match = line.match(/^(#{2,3})\s+(.+?)\s*#*\s*$/);
    if (!match) continue;
    const text = stripInline(match[2] ?? "");
    const base = slugify(text) || "section";
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    headings.push({
      depth: match[1] === "##" ? 2 : 3,
      text,
      id: count ? `${base}-${count + 1}` : base,
    });
  }
  return headings;
}

/** Plain text for the search index: no code blocks, no Markdown syntax. */
export function searchText(markdown: string): string {
  return [...proseLines(markdown)]
    .map((line) =>
      stripInline(
        line
          .replace(/^\s*>\s?/, "")
          .replace(/^\s*\[!\w+\]\s*/, "")
          .replace(/^\s*#{1,6}\s+/, "")
          .replace(/^\s*([-*+]|\d+\.)\s+/, "")
          .replace(/\|/g, " "),
      ),
    )
    .filter((line) => line && !/^[-:\s]+$/.test(line))
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

/** The tab label in a code block's info string (`tab="Label"`), if any. */
export function tabLabel(meta: string | null | undefined): string | null {
  return meta?.match(/(?:^|\s)tab="([^"]+)"/)?.[1] ?? null;
}

// A minimal mdast shape: enough to walk the tree without the mdast types.
interface MdNode {
  type: string;
  depth?: number;
  lang?: string | null;
  meta?: string | null;
  value?: string;
  children?: MdNode[];
  data?: { hName?: string; hProperties?: Record<string, unknown> };
}

/**
 * Remove the `[!KIND]` marker from a callout blockquote.
 *
 * Returns true when the blockquote was a callout, so the caller can show its
 * children as plain page content.
 */
function stripCalloutMarker(node: MdNode): boolean {
  const paragraph = node.children?.[0];
  const first = paragraph?.type === "paragraph" ? paragraph.children?.[0] : undefined;
  if (!paragraph || first?.type !== "text" || !first.value) return false;
  const match = first.value.match(/^\[!(\w+)\][ \t]*\n?/);
  const kind = match?.[1]?.toLowerCase();
  if (!match || !kind || !CALLOUT_KINDS.includes(kind)) return false;
  first.value = first.value.slice(match[0].length);
  if (!first.value) paragraph.children?.shift();
  if (!paragraph.children?.length) node.children?.shift();
  return true;
}

/**
 * Join adjacent `tab="…"` code blocks into one tab group node, and replace
 * each callout blockquote with its own children.
 *
 * A callout's children are grouped on their own, so a tab group never joins
 * code blocks from both sides of a callout boundary.
 */
function groupTabs(children: MdNode[]): MdNode[] {
  const out: MdNode[] = [];
  let open: MdNode | null = null;
  for (const child of children) {
    if (child.type === "blockquote" && stripCalloutMarker(child)) {
      out.push(...groupTabs(child.children ?? []));
      open = null;
      continue;
    }
    const label = child.type === "code" ? tabLabel(child.meta) : null;
    if (label && open) {
      open.children?.push(child);
      continue;
    }
    open = label
      ? {
          type: "docsTabs",
          data: { hName: "div", hProperties: { dataTabs: "" } },
          children: [child],
        }
      : null;
    out.push(open ?? child);
  }
  for (const group of out) {
    if (group.type !== "docsTabs") continue;
    const labels = (group.children ?? []).map((c) => tabLabel(c.meta) ?? "");
    group.data = { hName: "div", hProperties: { dataTabs: JSON.stringify(labels) } };
  }
  return out;
}

/**
 * The docs remark plugin: heading ids (from `extractHeadings`, in order),
 * plain callouts, code tab groups and the MCP clients block.
 */
export function remarkDocs(options: { headingIds: readonly string[] }) {
  return (tree: MdNode): void => {
    let next = 0;
    const visit = (node: MdNode) => {
      if (node.type === "heading" && (node.depth === 2 || node.depth === 3)) {
        const id = options.headingIds[next++];
        if (id) node.data = { ...node.data, hProperties: { ...node.data?.hProperties, id } };
      }
      if (node.type === "code" && node.lang === MCP_CLIENTS_BLOCK) {
        node.type = "docsMcpClients";
        delete node.value;
        node.data = { hName: "div", hProperties: { dataMcpClients: "" } };
        node.children = [];
      }
      if (node.children) {
        // A tab group's own code blocks must not be grouped again.
        if (node.type !== "docsTabs") node.children = groupTabs(node.children);
        node.children.forEach(visit);
      }
    };
    visit(tree);
  };
}
