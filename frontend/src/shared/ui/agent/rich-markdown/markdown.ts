import MarkdownIt from "markdown-it";
import {
  MarkdownParser,
  MarkdownSerializer,
  defaultMarkdownParser,
  defaultMarkdownSerializer,
  schema as baseSchema,
  type MarkdownSerializerState,
} from "prosemirror-markdown";
import { Schema, type Node as PMNode } from "prosemirror-model";

// The editor keeps the draft as Markdown text, the same text the plain
// composer sent. Tables and task lists stay plain text in the editor.
type NodeName =
  | "doc"
  | "paragraph"
  | "blockquote"
  | "horizontal_rule"
  | "heading"
  | "code_block"
  | "ordered_list"
  | "bullet_list"
  | "list_item"
  | "text"
  | "image"
  | "hard_break";
type MarkName = "em" | "strong" | "link" | "code" | "strike";

export const schema = new Schema({
  nodes: baseSchema.spec.nodes.update("hard_break", {
    ...baseSchema.spec.nodes.get("hard_break")!,
    leafText: () => "\n",
  }),
  marks: baseSchema.spec.marks
    .update("code", { ...baseSchema.spec.marks.get("code")!, inclusive: false })
    .addToEnd("strike", {
      parseDOM: [{ tag: "s" }, { tag: "del" }, { tag: "strike" }],
      toDOM: () => ["s", 0],
    }),
}) as unknown as Schema<NodeName, MarkName>;

const tokenizer = MarkdownIt("commonmark", { html: false }).enable("strikethrough");

// A single newline in a draft is a line break, as the chat shows it.
const parser = new MarkdownParser(schema, tokenizer, {
  ...defaultMarkdownParser.tokens,
  s: { mark: "strike" },
  softbreak: { node: "hard_break" },
});

// `@path` mentions go out as typed, so the agent and the mention menu see them.
const MENTION = /((?<=^|\s)@[^\s@]+)/;

/** Escape only the characters that would start Markdown syntax in plain text. */
export function escapeText(text: string, startOfLine: boolean): string {
  let out = text
    .replace(/\\(?=[\\`*_~[\]#>!-])/g, "\\\\")
    .replace(/`/g, "\\`")
    // A `*` between spaces cannot open emphasis, so `2 * 3` stays as typed.
    .replace(/\*/g, (m, i: number, str: string) =>
      /\s/.test(str[i - 1] ?? " ") && /\s/.test(str[i + 1] ?? " ") ? m : "\\*",
    )
    .replace(/~~/g, "\\~\\~")
    .replace(/_/g, (m, i: number, str: string) =>
      /[^\W_]/.test(str[i - 1] ?? "") && /[^\W_]/.test(str[i + 1] ?? "") ? m : "\\_",
    )
    .replace(/\[(?=[^\]]*\][([])/g, "\\[");
  if (startOfLine) {
    out = out
      .replace(/^[+*-](?=\s|$)/, "\\$&")
      .replace(/^>/, "\\>")
      .replace(/^(#{1,6})(?=\s|$)/, "\\$1")
      .replace(/^(\d+)([.)])(?=\s|$)/, "$1\\$2");
  }
  return out;
}

type LineState = MarkdownSerializerState & { atBlockStart?: boolean; out: string };

const serializer = new MarkdownSerializer(
  {
    ...defaultMarkdownSerializer.nodes,
    bullet_list(state, node) {
      state.renderList(node, "  ", () => "- ");
    },
    hard_break(state, node, parent, index) {
      for (let i = index + 1; i < parent.childCount; i++)
        if (parent.child(i).type !== node.type) {
          state.write("\n");
          return;
        }
    },
    text(state, node) {
      const s = state as LineState;
      if ((state as unknown as { inAutolink?: boolean }).inAutolink) {
        state.text(node.text ?? "", false);
        return;
      }
      (node.text ?? "").split(MENTION).forEach((part, i) => {
        if (!part) return;
        state.write();
        const startOfLine = Boolean(s.atBlockStart) || /(^|\n)[ >]*$/.test(s.out);
        state.text(i % 2 ? part : escapeText(part, startOfLine), false);
      });
    },
  },
  {
    ...defaultMarkdownSerializer.marks,
    strike: { open: "~~", close: "~~", mixable: true, expelEnclosingWhitespace: true },
  },
);

/** Parse a Markdown draft into an editor document. */
export function parseMarkdown(text: string): PMNode {
  return parser.parse(text) ?? schema.node("doc", null, [schema.node("paragraph")]);
}

/** Write an editor document back to the Markdown draft the composer sends. */
export function serializeMarkdown(doc: PMNode): string {
  return serializer.serialize(doc, { tightLists: true });
}

/**
 * Check a typed link and add `https://` when it has no scheme.
 *
 * Args:
 *   raw: the text in the link field.
 *   allowMail: also accept `mailto:` links, as the link popover does.
 *
 * Returns:
 *   The link to store, or null when it is not an HTTP(S) (or mailto) link.
 */
export function normalizeLink(raw: string, allowMail = false): string | null {
  const text = raw.trim();
  if (!text || /\s/.test(text)) return null;
  const href = /^[a-z][a-z\d+.-]*:/i.test(text) ? text : `https://${text}`;
  try {
    const url = new URL(href);
    if (url.protocol === "mailto:") return allowMail && url.pathname ? href : null;
    return (url.protocol === "http:" || url.protocol === "https:") && url.hostname ? href : null;
  } catch {
    return null;
  }
}
