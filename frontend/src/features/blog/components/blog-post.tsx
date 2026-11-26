/**
 * The /blog/<slug> post page body. Server component.
 *
 * The post text is English in every UI language, so the article sets
 * lang="en" and dir="ltr" and renders with the English docs chrome. Only the
 * back link and the author and date line follow the UI language.
 */

import * as React from "react";
import Link from "next/link";
import { ArrowsSplitIcon } from "@phosphor-icons/react/dist/ssr/ArrowsSplit";
import { ChartLineUpIcon } from "@phosphor-icons/react/dist/ssr/ChartLineUp";
import { ChatsCircleIcon } from "@phosphor-icons/react/dist/ssr/ChatsCircle";
import { CpuIcon } from "@phosphor-icons/react/dist/ssr/Cpu";
import { CubeIcon } from "@phosphor-icons/react/dist/ssr/Cube";
import { GaugeIcon } from "@phosphor-icons/react/dist/ssr/Gauge";
import { WrenchIcon } from "@phosphor-icons/react/dist/ssr/Wrench";
import { DOCS_CHROME, DocsMarkdown, extractHeadings } from "@/features/docs";
import { msg } from "@/shared/lib/messages";
import { blogPostFigures, blogPostSource, type BlogFigure } from "../lib/content";
import {
  LEVERS,
  POST_WORDS,
  linkCitations,
  parsePostBody,
  referenceId,
  type PostBlock,
} from "../lib/post-body";
import { blogHref, type BlogPost } from "../lib/posts";
import { BlogFigureBehavior } from "./blog-figures";
import { BlogMeta } from "./blog-meta";
import "./blog-figures.css";

const CHROME = DOCS_CHROME.en;
const LINK = "font-medium text-primary underline-offset-4 hover:underline";
/** The icon of each lever, in the order of LEVERS, as in the essay. */
const LEVER_ICONS = [
  CpuIcon,
  GaugeIcon,
  ArrowsSplitIcon,
  ChatsCircleIcon,
  WrenchIcon,
  CubeIcon,
  ChartLineUpIcon,
];

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
      <a key={i} href={`#${referenceId(Number(n))}`} className="cite">
        {part}
      </a>
    ) : (
      <React.Fragment key={i}>{part}</React.Fragment>
    );
  });
}

/**
 * One live essay figure: the dark chart card, wider than the text, with its
 * numbered caption inside the card, so the zoom view carries the caption too.
 * A figure with no caption has no number and no caption line.
 */
function Figure({
  block,
  figure,
}: {
  block: Extract<PostBlock, { kind: "figure" }>;
  figure?: BlogFigure;
}) {
  if (!figure) return null;
  const zoomable = figure.html.includes('class="zbtn"');
  const label = `fh-${block.id}`;
  return (
    <figure
      id={block.number === null ? undefined : `figure-${block.number}`}
      className="bf not-prose my-12 scroll-mt-6"
      aria-labelledby={figure.html.includes(`id="${label}"`) ? label : undefined}
    >
      <div className={zoomable ? "card zoomable" : "card"} id={`fig-${block.id}`}>
        {/* The HTML is the essay's own figure export, a trusted file in this repository. */}
        <div className="contents" dangerouslySetInnerHTML={{ __html: figure.html }} />
        {block.number !== null && (
          <figcaption>
            <b>
              {POST_WORDS.figure} {block.number}.
            </b>{" "}
            <CaptionText text={block.caption} />
          </figcaption>
        )}
      </div>
    </figure>
  );
}

/** A heading that names one or more of the seven levers, each with its icon and its short description. */
function LeverHeading({ levers }: { levers: number[] }) {
  return (
    <div className="not-prose mt-10 mb-3.5 space-y-4">
      {levers.map((n) => {
        const Icon = LEVER_ICONS[n - 1];
        const lever = LEVERS[n - 1];
        if (!Icon || !lever) return null;
        return (
          <div
            key={n}
            className="grid grid-cols-[1.625rem_minmax(0,1fr)] items-center gap-x-[11px] gap-y-1"
          >
            <span
              aria-hidden="true"
              className="inline-grid size-[1.625rem] place-items-center rounded-[7px] border border-primary/30 bg-primary/10 text-primary"
            >
              <Icon size={15} weight="regular" />
            </span>
            <h3
              id={`lever-${n}`}
              className="m-0 flex scroll-mt-6 items-center gap-3 font-display text-lg leading-tight font-semibold tracking-[-0.01em] text-foreground after:h-px after:flex-1 after:bg-linear-to-r after:from-border after:to-transparent"
            >
              {lever.name}
            </h3>
            <p className="col-span-full m-0 text-sm leading-[1.45] text-muted-foreground">
              {lever.description}
            </p>
          </div>
        );
      })}
    </div>
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
              return <Figure key={i} block={block} figure={figures[block.id]} />;
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
      <BlogFigureBehavior />
    </div>
  );
}
