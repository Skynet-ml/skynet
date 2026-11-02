/** /llms.txt: a short map of the docs for language models (https://llmstxt.org). */

import { DOCS_LOCALES, DOCS_PAGES, DOCS_SECTIONS, docsMarkdownHref } from "@/features/docs";
import { getSiteUrl, serviceUrls } from "@/shared/lib/site-config";

export async function GET() {
  const site = getSiteUrl();
  const sections = DOCS_SECTIONS.map((section) => {
    const pages = DOCS_PAGES.filter((p) => p.section === section.id).map(
      (p) => `- [${p.title.en}](${site}${docsMarkdownHref(p.slug, "en")}): ${p.description.en}`,
    );
    return `## ${section.title.en}\n\n${pages.join("\n")}`;
  });
  const body = [
    "# Skynet",
    "",
    "> Skynet optimizes prompts, programs, agents and other artifacts against your own data and scorer. Skynet is Optimize Anything: one product for prompts, code, text and DSPy programs, with native engines and DSPy optimizers.",
    "",
    `Every page below is Markdown. The full docs are in one file at ${site}/llms-full.txt. The hosted MCP server is at ${serviceUrls(site).mcp}. Other languages are under ${site}/docs/<locale>, for these locales: ${DOCS_LOCALES.filter((l) => l !== "en").join(", ")}.`,
    "",
    ...sections.flatMap((s) => [s, ""]),
  ].join("\n");
  return new Response(body, {
    headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "public, max-age=3600" },
  });
}
