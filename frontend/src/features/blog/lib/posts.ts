/**
 * The blog post list. Post text is English for every UI language, so the
 * title and summary are plain English strings, not catalog keys.
 *
 * Kept free of `@/` imports so `node --test` can load it.
 */

export interface BlogPost {
  slug: string;
  title: string;
  summary: string;
  author: string;
  /** ISO `YYYY-MM-DD`. */
  date: string;
}

/** Newest first. */
export const BLOG_POSTS: readonly BlogPost[] = [
  {
    slug: "the-most-valuable-skill-of-this-century",
    title: "The most valuable skill of this century",
    summary:
      "Seven levers, cheaper intelligence, first-principles thinking, and agent-swarm feedback loops.",
    author: "Gilad Morad",
    date: "2026-11-22",
  },
];

/** The URL path of the blog index, or of one post. */
export function blogHref(slug = ""): string {
  return slug ? `/blog/${slug}` : "/blog";
}

/** The post with this slug, if any. */
export function findBlogPost(slug: string): BlogPost | undefined {
  return BLOG_POSTS.find((post) => post.slug === slug);
}
