/**
 * Split a post's Markdown into the blocks the post page renders.
 *
 * Post-only syntax, on a line of its own:
 * - `{{figure: <id> | <caption>}}` shows live figure <id> from content/<slug>.figures.json with its caption.
 * - `{{lever: 1}}` or `{{lever: 2, 4}}` is a heading that names the levers.
 * `## References` starts the numbered source list; a `[n]` in the text links to source n.
 *
 * Kept free of `@/` imports so `node --test` can load it.
 */

export type PostBlock =
  | { kind: "markdown"; text: string }
  | { kind: "figure"; id: string; caption: string; number: number }
  | { kind: "lever"; levers: number[] };

export interface PostReference {
  number: number;
  text: string;
  notes: string[];
}

export interface PostBody {
  blocks: PostBlock[];
  references: PostReference[];
  /** Markdown after the source list, such as the portrait credits. */
  afterReferences: string;
}

/** The seven levers of the essay, in the order of its Figure 2. */
export const LEVERS = [
  { name: "Model", description: "For example, GPT-6 Astra or Claude Opus 5" },
  { name: "Reasoning effort", description: "For example, low, high, or max" },
  { name: "Multiple agents", description: "Running multiple tries and combining different models" },
  { name: "Agent communication", description: "How agents coordinate and share information" },
  { name: "Harness", description: "The tools and the context management" },
  { name: "Environment", description: "The basic interaction the model has with the problem" },
  { name: "Metric", description: "The methodology for scoring and measuring a result" },
] as const;

/** Words of the post itself, so English in every UI language (not catalog text). */
export const POST_WORDS = { figure: "Figure", references: "References" } as const;

const MARKER = /^\{\{(figure|lever):\s*(.+?)\}\}\s*$/;

/** The anchor id of source `n` in the reference list. */
export function referenceId(n: number): string {
  return `ref-${n}`;
}

/** Turn each `[n]` citation into a link to source n. Markdown links (`[text](url)`) stay as they are. */
export function linkCitations(markdown: string): string {
  return markdown.replace(
    /\[(\d+)\](?!\()/g,
    (_, n: string) => `[\\[${n}\\]](#${referenceId(Number(n))})`,
  );
}

/** Parse the text under `## References`: numbered sources with indented `- note` lines, then free Markdown. */
function parseReferences(block: string): Pick<PostBody, "references" | "afterReferences"> {
  const references: PostReference[] = [];
  const after: string[] = [];
  for (const line of block.split("\n")) {
    const source = line.match(/^(\d+)\.\s+(.*)$/);
    const note = line.match(/^\s+-\s+(.*)$/);
    if (source && after.length === 0)
      references.push({ number: Number(source[1]), text: source[2] ?? "", notes: [] });
    else if (note && references.length && after.length === 0)
      references.at(-1)?.notes.push(note[1] ?? "");
    else if (line.trim() || after.length) after.push(line);
  }
  return { references, afterReferences: after.join("\n").trim() };
}

/** Split a post's Markdown body into text, figure and lever blocks, and its reference list. */
export function parsePostBody(source: string): PostBody {
  const [main = "", refs = ""] = source.split(/^## References\s*$/m);
  const blocks: PostBlock[] = [];
  let text: string[] = [];
  let figures = 0;
  const flush = () => {
    const joined = text.join("\n").trim();
    if (joined) blocks.push({ kind: "markdown", text: joined });
    text = [];
  };
  for (const line of main.split("\n")) {
    const marker = line.match(MARKER);
    if (!marker) {
      text.push(line);
      continue;
    }
    flush();
    const value = marker[2] ?? "";
    if (marker[1] === "figure") {
      const [id = "", ...caption] = value.split("|");
      blocks.push({
        kind: "figure",
        id: id.trim(),
        caption: caption.join("|").trim(),
        number: ++figures,
      });
    } else {
      blocks.push({ kind: "lever", levers: value.split(",").map((n) => Number(n.trim())) });
    }
  }
  flush();
  return { blocks, ...parseReferences(refs) };
}
