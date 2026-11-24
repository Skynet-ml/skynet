import assert from "node:assert/strict";
import test from "node:test";
import { existsSync, readFileSync } from "node:fs";
import { LEVERS, linkCitations, parsePostBody } from "./post-body.ts";
import { BLOG_POSTS, blogHref, findBlogPost } from "./posts.ts";

const SLUG = "the-most-valuable-skill-of-this-century";

/** A post's Markdown source, read the way the content loader bundles it. */
function source(slug: string): string {
  return readFileSync(new URL(`../content/${slug}.md`, import.meta.url), "utf8");
}

test("the index lists the essay and the loader finds it", () => {
  assert.ok(BLOG_POSTS.some((post) => post.slug === SLUG));
  assert.equal(findBlogPost(SLUG)?.title, "The most valuable skill of this century");
  assert.equal(findBlogPost("missing"), undefined);
  assert.equal(blogHref(), "/blog");
  assert.equal(blogHref(SLUG), `/blog/${SLUG}`);
});

test("every post has a source, and every figure marker has its HTML", () => {
  for (const post of BLOG_POSTS) {
    const body = parsePostBody(source(post.slug));
    const manifest = JSON.parse(
      readFileSync(new URL(`../content/${post.slug}.figures.json`, import.meta.url), "utf8"),
    ) as Record<string, { html?: string }>;
    const figures = body.blocks.filter((block) => block.kind === "figure");
    assert.ok(figures.length > 0);
    for (const figure of figures) {
      const html = manifest[figure.id]?.html ?? "";
      assert.match(html, /^<div class="card-body">/, figure.id);
      // Images are files in public/, so the page does not carry them inline two times.
      assert.doesNotMatch(html, /data:image\//, figure.id);
      for (const [, src] of html.matchAll(/href="(\/[^"]+\.(?:jpg|webp|png))"/g)) {
        assert.ok(existsSync(new URL(`../../../../public${src}`, import.meta.url)), src);
      }
    }
  }
});

test("the essay parses into figures, levers and numbered sources", () => {
  const body = parsePostBody(source(SLUG));
  const figures = body.blocks.filter((block) => block.kind === "figure");
  assert.deepEqual(
    figures.map((f) => f.number),
    figures.map((_, i) => i + 1),
  );
  for (const block of body.blocks) {
    if (block.kind === "lever")
      for (const n of block.levers) assert.ok(LEVERS[n - 1], `lever ${n}`);
    if (block.kind === "markdown") assert.doesNotMatch(block.text, /\{\{/);
  }
  assert.deepEqual(
    body.references.map((r) => r.number),
    body.references.map((_, i) => i + 1),
  );
  assert.match(body.afterReferences, /Wikimedia Commons/);
});

test("citations link to their source, Markdown links stay", () => {
  assert.equal(linkCitations("as [5] said"), "as [\\[5\\]](#ref-5) said");
  assert.equal(linkCitations("[text](https://x.y)"), "[text](https://x.y)");
});
