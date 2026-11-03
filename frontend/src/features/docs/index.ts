/** Public surface of the docs feature: the page frame, the renderer and the content. */

export { DocsMarkdown } from "./components/docs-markdown";
export { DocsShell } from "./components/docs-shell";
export { DOCS_CHROME } from "./lib/chrome";
export { DOCS_PATH_HEADER, docsPathLocale, pickRequestLocale } from "./lib/docs-answer";
export { docsMarkdownDocument, docsSearchIndex, docsSource } from "./lib/content";
export { extractHeadings } from "./lib/markdown";
export {
  DOCS_LOCALES,
  DOCS_PAGES,
  DOCS_SECTIONS,
  docsDir,
  docsHref,
  docsMarkdownHref,
  docsMarkdownSegments,
  isDocsLocale,
  resolveDocsPath,
  type DocsLocale,
} from "./lib/nav";
