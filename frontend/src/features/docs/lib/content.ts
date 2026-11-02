/**
 * Server-side access to the docs Markdown sources.
 *
 * The pages are plain Markdown files under features/docs/content/<locale>/.
 * Turbopack bundles them as strings (the `?raw` rule in next.config.ts), so
 * the server reads no files at run time and the standalone build needs no
 * extra tracing.
 */

import { MCP_CLIENTS_BLOCK, searchText, extractHeadings } from "./markdown";
import { DOCS_PAGES, docsHref, localizeDocsLinks, type DocsLocale, type DocsPage } from "./nav";
import { mcpClientsMarkdown } from "@/shared/lib/mcp-clients";
import { getSiteUrl, serviceUrls, withServiceUrls } from "@/shared/lib/site-config";

// Whole modules, not `import: "default"`: with that option the production
// build renames the text modules' default export but not the glob's lookup,
// and every page reads undefined.
const MODULES = import.meta.glob<{ default: string }>("../content/**/*.md", {
  query: "?raw",
  eager: true,
});
const SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(MODULES).map(([path, module]) => [path, module.default]),
);

/**
 * A page's Markdown source in a locale. A page that has no translation yet
 * falls back to English.
 */
export function docsSource(page: DocsPage, locale: DocsLocale): string {
  const name = page.slug || "index";
  const source = SOURCES[`../content/${locale}/${name}.md`] ?? SOURCES[`../content/en/${name}.md`];
  if (source === undefined) throw new Error(`Missing docs source: ${name}.md`);
  return withServiceUrls(localizeDocsLinks(source, locale));
}

/** Replace the MCP clients placeholder with the generated client sections. */
function expandBlocks(markdown: string, locale: DocsLocale): string {
  const block = new RegExp(`\`\`\`${MCP_CLIENTS_BLOCK}\\n\`\`\``, "g");
  return markdown.replace(block, () => mcpClientsMarkdown(locale, serviceUrls().mcp));
}

/**
 * The page as one self-contained Markdown document: title, summary, body and
 * a link back to the HTML page. Served at /docs/<page>.md and in llms-full.txt.
 */
export function docsMarkdownDocument(page: DocsPage, locale: DocsLocale): string {
  const url = `${getSiteUrl()}${docsHref(page.slug, locale)}`;
  const body = expandBlocks(docsSource(page, locale), locale).trim();
  return `# ${page.title[locale]}\n\n> ${page.description[locale]}\n\n${body}\n\n---\n\nSource: ${url}\n`;
}

export interface DocsSearchEntry {
  href: string;
  title: string;
  section: string;
  headings: Array<{ id: string; text: string }>;
  text: string;
}

/** The search index for one locale: titles, headings and plain body text. */
export function docsSearchIndex(
  locale: DocsLocale,
  sectionTitle: (page: DocsPage) => string,
): DocsSearchEntry[] {
  return DOCS_PAGES.map((page) => {
    const source = docsSource(page, locale);
    return {
      href: docsHref(page.slug, locale),
      title: page.title[locale],
      section: sectionTitle(page),
      headings: extractHeadings(source).map(({ id, text }) => ({ id, text })),
      text: `${page.description[locale]} ${searchText(source)}`,
    };
  });
}
