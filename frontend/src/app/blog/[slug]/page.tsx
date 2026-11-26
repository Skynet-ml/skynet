/** One public blog post at /blog/<slug>. No sign-in. */

import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { BlogPostView, BlogShell, blogHref, findBlogPost } from "@/features/blog";
import { APP_NAME } from "@/shared/lib/brand";
import { getSiteUrl } from "@/shared/lib/site-config";
import { pinRequestLocale } from "../../request-locale";

type Props = { params: Promise<{ slug: string }> };

// The post metadata is the post's own English text, so it needs no catalog
// lookup and no wait for the request locale.
export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const post = findBlogPost((await params).slug);
  if (!post) return {};
  const url = blogHref(post.slug);
  return {
    title: post.title,
    description: post.summary,
    authors: [{ name: post.author }],
    alternates: { canonical: url },
    openGraph: {
      type: "article",
      siteName: APP_NAME,
      title: post.title,
      description: post.summary,
      url,
      locale: "en_US",
      publishedTime: post.date,
      authors: [post.author],
    },
    twitter: { card: "summary", title: post.title, description: post.summary },
    robots: { index: true, follow: true },
  };
}

export default async function BlogPostPage({ params }: Props) {
  await pinRequestLocale();
  const post = findBlogPost((await params).slug);
  if (!post) notFound();
  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "BlogPosting",
    headline: post.title,
    description: post.summary,
    datePublished: post.date,
    author: { "@type": "Person", name: post.author },
    inLanguage: "en",
    url: `${getSiteUrl()}${blogHref(post.slug)}`,
  };
  return (
    <BlogShell>
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c") }}
      />
      <BlogPostView post={post} />
    </BlogShell>
  );
}
