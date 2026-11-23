/** Public blog index at /blog. No sign-in. */

import type { Metadata, ResolvingMetadata } from "next";
import { BlogIndex, BlogShell, blogHref } from "@/features/blog";
import { APP_NAME } from "@/shared/lib/brand";
import { msg } from "@/shared/lib/messages";
import { pinRequestLocale } from "../request-locale";

// Wait for the root layout's metadata first: it pins the request locale that
// msg() reads (see app/privacy/page.tsx).
export async function generateMetadata(
  _props: object,
  parent: ResolvingMetadata,
): Promise<Metadata> {
  await parent;
  const title = msg("blog.title");
  const description = msg("blog.meta_description");
  return {
    title,
    description,
    alternates: { canonical: blogHref() },
    openGraph: { type: "website", siteName: APP_NAME, title, description, url: blogHref() },
    robots: { index: true, follow: true },
  };
}

export default async function BlogIndexPage() {
  await pinRequestLocale();
  return (
    <BlogShell>
      <BlogIndex />
    </BlogShell>
  );
}
