/**
 * Syntax colours for a file view, without a code editor: the grammar
 * is picked by file name from the editor's own catalogue and loaded on
 * demand. Imported lazily, so a page pays for it only when it shows a file.
 */

import { LanguageDescription } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { highlightCode, tagHighlighter } from "@lezer/highlight";
import { CODE_HIGHLIGHT_SPECS } from "@/shared/ui/code-highlight-style";

export interface HighlightToken {
  text: string;
  /** Index into CODE_HIGHLIGHT_SPECS; null for plain text. */
  spec: number | null;
}

const highlighter = tagHighlighter(
  CODE_HIGHLIGHT_SPECS.map((spec, i) => ({ tag: spec.tag, class: String(i) })),
);

/** Tokens per line of `text`, or null when no grammar matches the file name. */
export async function highlightLines(
  path: string,
  text: string,
): Promise<HighlightToken[][] | null> {
  const name = path.slice(path.lastIndexOf("/") + 1);
  const description = LanguageDescription.matchFilename(languages, name);
  if (!description) return null;
  const support = await description.load();
  const tree = support.language.parser.parse(text);
  const lines: HighlightToken[][] = [[]];
  highlightCode(
    text,
    tree,
    highlighter,
    (chunk, classes) => {
      // Nested tags come back space-separated; the innermost wins.
      const last = classes.split(" ").filter(Boolean).pop();
      lines[lines.length - 1]?.push({ text: chunk, spec: last == null ? null : Number(last) });
    },
    () => lines.push([]),
  );
  return lines;
}
