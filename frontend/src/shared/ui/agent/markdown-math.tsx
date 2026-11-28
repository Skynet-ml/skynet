"use client";

/**
 * Draws TeX with KaTeX. The message renderer loads this module only when a
 * reply holds math, so KaTeX and its fonts stay out of every other chat.
 */

import * as React from "react";
import katex from "katex";
import "katex/dist/katex.min.css";

/**
 * Render one formula, or show its source when KaTeX cannot parse it.
 *
 * Args:
 *   tex: The formula without its delimiters.
 *   display: True for a block formula, false for one inside a sentence.
 *   onError: Called after a render that KaTeX cannot parse, so a caller can show other markup.
 */
export default function MarkdownMath({
  tex,
  display,
  onError,
}: {
  tex: string;
  display: boolean;
  onError?: () => void;
}) {
  let html: string | null = null;
  try {
    // KaTeX escapes the TeX it emits and runs with trust off, so its HTML is safe.
    html = katex.renderToString(tex, { displayMode: display, throwOnError: true });
  } catch {
    // The caller's state can change only after this render, not during it.
  }
  const failed = html === null;
  React.useEffect(() => {
    if (failed) onError?.();
  }, [failed, onError]);
  if (html === null) {
    // As in Codex, a formula KaTeX cannot parse shows its source.
    return display ? (
      <pre dir="ltr" className="md-math whitespace-pre-wrap text-left">
        <code>{tex}</code>
      </pre>
    ) : (
      <code dir="ltr">{tex}</code>
    );
  }
  return display ? (
    <div dir="ltr" className="md-math" dangerouslySetInnerHTML={{ __html: html }} />
  ) : (
    <span dir="ltr" dangerouslySetInnerHTML={{ __html: html }} />
  );
}
