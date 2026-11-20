"use client";

/**
 * Draws TeX with KaTeX. The message renderer loads this module only when a
 * reply holds math, so KaTeX and its fonts stay out of every other chat.
 */

import katex from "katex";
import "katex/dist/katex.min.css";

/**
 * Render one formula, or show its source when KaTeX cannot parse it.
 *
 * Args:
 *   tex: The formula without its delimiters.
 *   display: True for a block formula, false for one inside a sentence.
 */
export default function MarkdownMath({ tex, display }: { tex: string; display: boolean }) {
  // KaTeX escapes the TeX it emits and runs with trust off, so its HTML is safe.
  const html = katex.renderToString(tex, { displayMode: display, throwOnError: false });
  return display ? (
    <div dir="ltr" className="md-math" dangerouslySetInnerHTML={{ __html: html }} />
  ) : (
    <span dir="ltr" dangerouslySetInnerHTML={{ __html: html }} />
  );
}
