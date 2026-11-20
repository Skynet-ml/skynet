/**
 * Syntax colours for a chat code block, picked by the fence's language name
 * from the code editor's grammar catalogue. Imported lazily, so a chat pays
 * for it only when a reply holds code.
 */

import { LanguageDescription } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { highlightCode, tagHighlighter } from "@lezer/highlight";
import { CODE_HIGHLIGHT_SPECS } from "@/shared/ui/code-highlight-style";

export interface CodeToken {
  text: string;
  /** Inline colour and type style; absent for plain text. */
  style?: { color: string; fontWeight?: "bold"; fontStyle?: "italic" };
}

const PLAIN = new Set(["text", "plaintext", "plain", "txt"]);

const highlighter = tagHighlighter(
  CODE_HIGHLIGHT_SPECS.map((spec, i) => ({ tag: spec.tag, class: String(i) })),
);

function findLanguage(name: string): LanguageDescription | null {
  return (
    LanguageDescription.matchLanguageName(languages, name) ??
    LanguageDescription.matchFilename(languages, `file.${name}`)
  );
}

/**
 * Tokens for `code`, or null when no grammar matches the language name.
 *
 * Args:
 *   language: The fence's first word, such as "ts" or "python".
 *   code: The block's text.
 */
export async function highlightTokens(language: string, code: string): Promise<CodeToken[] | null> {
  const name = language.toLowerCase();
  const description = PLAIN.has(name) ? null : findLanguage(name);
  if (!description) return null;
  const support = await description.load();
  const tree = support.language.parser.parse(code);
  const tokens: CodeToken[] = [];
  highlightCode(
    code,
    tree,
    highlighter,
    (text, classes) => {
      // Nested tags come back space-separated; the innermost wins.
      const last = classes.split(" ").filter(Boolean).pop();
      const spec = last == null ? undefined : CODE_HIGHLIGHT_SPECS[Number(last)];
      tokens.push(
        spec
          ? {
              text,
              style: { color: spec.color, fontWeight: spec.fontWeight, fontStyle: spec.fontStyle },
            }
          : { text },
      );
    },
    () => tokens.push({ text: "\n" }),
  );
  return tokens;
}
