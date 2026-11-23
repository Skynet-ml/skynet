/** The /blog post list. Server component. */

import Link from "next/link";
import { msg } from "@/shared/lib/messages";
import { BLOG_POSTS, blogHref } from "../lib/posts";
import { BlogMeta } from "./blog-meta";

/** Render the blog title and one card for each post. */
export function BlogIndex() {
  return (
    <div className="mx-auto max-w-[68ch]">
      <h1 className="font-display text-3xl font-semibold tracking-tight sm:text-4xl bg-none! [-webkit-text-fill-color:currentColor]!">
        {msg("blog.title")}
      </h1>
      <p className="mt-2 text-muted-foreground">{msg("blog.tagline")}</p>
      <ul className="mt-10 grid gap-4">
        {BLOG_POSTS.map((post) => (
          <li key={post.slug}>
            <article className="rounded-xl border border-border bg-card p-6 transition-colors hover:border-primary/50">
              <BlogMeta post={post} />
              <h2
                lang="en"
                dir="ltr"
                className="mt-3 font-display text-2xl font-semibold tracking-tight"
              >
                <Link
                  href={blogHref(post.slug)}
                  className="outline-none hover:text-primary focus-visible:underline"
                >
                  {post.title}
                </Link>
              </h2>
              <p lang="en" dir="ltr" className="mt-2 leading-7 text-foreground/75">
                {post.summary}
              </p>
              <Link
                href={blogHref(post.slug)}
                aria-label={`${msg("blog.read")}: ${post.title}`}
                className="mt-4 inline-flex min-h-[44px] items-center text-sm font-medium text-primary underline-offset-4 hover:underline"
              >
                {msg("blog.read")}
              </Link>
            </article>
          </li>
        ))}
      </ul>
    </div>
  );
}
