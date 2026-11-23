/** The author and date line of a post, in the UI language. */

import { msg } from "@/shared/lib/messages";
import { dirForLocale } from "@/shared/lib/locale";
import { getActiveIntlLocale, getActiveLocale } from "@/shared/lib/runtime-locale";
import type { BlogPost } from "../lib/posts";

/** Format an ISO `YYYY-MM-DD` date in the UI language. */
function formatPostDate(isoDate: string): string {
  return new Intl.DateTimeFormat(getActiveIntlLocale(), {
    dateStyle: "long",
    timeZone: "UTC",
  }).format(new Date(`${isoDate}T00:00:00Z`));
}

/**
 * Render "By <author> · <date>" for a post. It sets its own lang and dir,
 * because on the post page it sits inside the English article.
 */
export function BlogMeta({ post }: { post: BlogPost }) {
  const locale = getActiveLocale();
  return (
    <p
      lang={locale}
      dir={dirForLocale(locale)}
      className="font-mono text-xs uppercase tracking-wide text-muted-foreground"
    >
      <span>{msg("blog.by_author", { author: post.author })}</span>
      <span aria-hidden="true"> · </span>
      <time dateTime={post.date}>{formatPostDate(post.date)}</time>
    </p>
  );
}
