/**
 * Server-side access to the blog post sources.
 *
 * Each post is content/<slug>.md, bundled as a string by the same `?raw`
 * Turbopack rule the docs use, plus content/<slug>.figures.json: the size
 * and alt text of each figure SVG under public/blog/<slug>/.
 */

import type { BlogPost } from "./posts";

export interface BlogFigure {
  title: string;
  alt: string;
  width: number;
  height: number;
}

// Whole modules, not `import: "default"`: see features/docs/lib/content.ts.
const SOURCES = import.meta.glob<{ default: string }>("../content/*.md", {
  query: "?raw",
  eager: true,
});
const FIGURES = import.meta.glob<{ default: Record<string, BlogFigure> }>(
  "../content/*.figures.json",
  {
    eager: true,
  },
);

/** A post's Markdown body. */
export function blogPostSource(post: BlogPost): string {
  const source = SOURCES[`../content/${post.slug}.md`]?.default;
  if (source === undefined) throw new Error(`Missing blog source: ${post.slug}.md`);
  return source;
}

/** A post's figures by id. */
export function blogPostFigures(post: BlogPost): Record<string, BlogFigure> {
  return FIGURES[`../content/${post.slug}.figures.json`]?.default ?? {};
}
