/**
 * The /blog/<slug> post page body. Server component.
 *
 * The post text is English in every UI language, so the article sets
 * lang="en" and dir="ltr" and renders with the English docs chrome. Only the
 * back link and the author and date line follow the UI language.
 */

import * as React from "react";
import Link from "next/link";
import { DOCS_CHROME, DocsMarkdown, extractHeadings } from "@/features/docs";
import { msg } from "@/shared/lib/messages";
import { blogPostFigures, blogPostSource, type BlogFigure } from "../lib/content";
import {
  LEVER_NAMES,
  POST_WORDS,
  linkCitations,
  parsePostBody,
  referenceId,
  type PostBlock,
} from "../lib/post-body";
import { blogFigureSrc, blogHref, type BlogPost } from "../lib/posts";
import { BlogMeta } from "./blog-meta";

const CHROME = DOCS_CHROME.en;
const LINK = "font-medium text-primary underline-offset-4 hover:underline";

/** Render Markdown with `[n]` citations linked to the source list. */
function Prose({ source }: { source: string }) {
  const linked = linkCitations(source);
  const ids = extractHeadings(linked).map((h) => h.id);
  return <DocsMarkdown source={linked} headingIds={ids} locale="en" chrome={CHROME} />;
}

/** Plain caption text with each `[n]` as a link to source n. */
function CaptionText({ text }: { text: string }) {
  return text.split(/(\[\d+\])/).map((part, i) => {
    const n = part.match(/^\[(\d+)\]$/)?.[1];
    return n ? (
      <a key={i} href={`#${referenceId(Number(n))}`} className={LINK}>
        {part}
      </a>
    ) : (
      <React.Fragment key={i}>{part}</React.Fragment>
    );
  });
}

/** One exported essay figure with its numbered caption. */
function Figure({
  slug,
  block,
  figure,
}: {
  slug: string;
  block: Extract<PostBlock, { kind: "figure" }>;
  figure?: BlogFigure;
}) {
  return (
    <figure id={`figure-${block.number}`} className="my-10 scroll-mt-6">
      {/* A plain <img>: next/image adds nothing for a static SVG. The figures
          are dark cards that read on both themes; the border marks their
          edge on the dark theme. */}
      <img
        src={blogFigureSrc(slug, block.id)}
        alt={figure?.alt || figure?.title || block.caption}
        width={figure?.width}
        height={figure?.height}
        loading={block.number > 1 ? "lazy" : undefined}
        decoding="async"
        className="h-auto w-full rounded-xl border border-border"
      />
      <figcaption className="mt-3 text-sm leading-6 text-muted-foreground">
        <span className="font-mono text-xs font-semibold uppercase tracking-wide text-foreground">
          {POST_WORDS.figure} {block.number}.
        </span>{" "}
        <CaptionText text={block.caption} />
      </figcaption>
    </figure>
  );
}

/** A heading that names one or more of the six levers, with their numbers as badges. */
function LeverHeading({ levers }: { levers: number[] }) {
  return (
    <h3 id={`lever-${levers.join("-")}`} className="flex flex-wrap items-center gap-x-4 gap-y-2">
      {levers.map((n) => (
        <span key={n} className="inline-flex items-center gap-2">
          <span className="inline-flex size-7 items-center justify-center rounded-md border border-border font-mono text-sm">
            {n}
          </span>
          {LEVER_NAMES[n - 1]}
        </span>
      ))}
    </h3>
  );
}

/** Render one post: title, byline, body, figures, sources and credits. */
export function BlogPostView({ post }: { post: BlogPost }) {
  const body = parsePostBody(blogPostSource(post));
  const figures = blogPostFigures(post);
  return (
    <div className="mx-auto max-w-[36rem]">
      <Link href={blogHref()} className={`text-sm ${LINK}`}>
        {msg("blog.back")}
      </Link>
      <article lang="en" dir="ltr" className="mt-8">
        <header className="mb-10">
          <h1 className="font-display text-3xl font-semibold tracking-tight text-balance sm:text-5xl bg-none! [-webkit-text-fill-color:currentColor]!">
            {post.title}
          </h1>
          <div className="mt-4">
            <BlogMeta post={post} />
          </div>
        </header>
        <div className="docs-prose [&_h2]:text-balance [&_h3]:text-balance [&_li]:text-pretty [&_p]:text-pretty">
          {body.blocks.map((block, i) => {
            if (block.kind === "figure")
              return <Figure key={i} slug={post.slug} block={block} figure={figures[block.id]} />;
            if (block.kind === "lever") return <LeverHeading key={i} levers={block.levers} />;
            return <Prose key={i} source={block.text} />;
          })}
          {body.references.length > 0 && (
            <section aria-labelledby="references">
              <h2 id="references">{POST_WORDS.references}</h2>
              <ol className="text-sm leading-6">
                {body.references.map((ref) => (
                  <li
                    key={ref.number}
                    id={referenceId(ref.number)}
                    value={ref.number}
                    className="scroll-mt-6"
                  >
                    <DocsMarkdown source={ref.text} locale="en" chrome={CHROME} />
                    {ref.notes.length > 0 && (
                      <ul className="text-muted-foreground">
                        {ref.notes.map((note, i) => (
                          <li key={i}>
                            <DocsMarkdown source={note} locale="en" chrome={CHROME} />
                          </li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ol>
            </section>
          )}
          {body.afterReferences && (
            <div className="mt-10 border-t border-border pt-6 text-sm leading-6 text-muted-foreground">
              <DocsMarkdown source={body.afterReferences} locale="en" chrome={CHROME} />
            </div>
          )}
        </div>
      </article>
    </div>
  );
}
