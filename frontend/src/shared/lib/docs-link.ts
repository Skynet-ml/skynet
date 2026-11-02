/**
 * Links to the Skynet docs.
 *
 * The Mac app serves its UI from a local server, so a relative `/docs` link
 * would open the docs inside the app window (or do nothing for a new tab,
 * because the app window opens no extra windows). In local mode a docs link
 * points at the hosted web docs instead, and the Mac app opens that URL in
 * the default browser. The hosted app keeps its relative links.
 */

import { isLocalMode } from "@/shared/lib/runtime-env";
import { hostedAppUrl } from "@/shared/lib/share-link";

// /docs, /docs/<page>, /docs#x, /docs?x and the Markdown twin /docs.md, but not
// /docs-search or /docs-md.
const DOCS_PATH = /^\/docs(?:[/?#.]|$)/;

/** Whether `href` is a relative link to the Skynet docs. */
export function isDocsPath(href: string): boolean {
  return DOCS_PATH.test(href);
}

/**
 * The href to use for a link. In local mode a relative docs link becomes the
 * hosted docs URL; every other link stays as it is.
 */
export function docsLinkHref(href: string, local: boolean = isLocalMode()): string {
  if (!local || !isDocsPath(href)) return href;
  return hostedAppUrl(href) ?? href;
}

/**
 * Anchor props for a docs link: the href, plus a new browser tab when the link
 * leaves the app (local mode).
 */
export function docsLinkProps(href: string, local: boolean = isLocalMode()): { href: string; target?: "_blank"; rel?: string } {
  const resolved = docsLinkHref(href, local);
  return resolved === href ? { href } : { href: resolved, target: "_blank", rel: "noopener noreferrer" };
}
