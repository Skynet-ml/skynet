/**
 * Pure text steps that run on a chat message before the Markdown parser sees
 * it: math delimiters, the repair of a half-streamed tail, and small
 * helpers the renderer needs.
 */

/** Marks an inline code span that holds TeX, so the renderer draws math. */
export const INLINE_MATH_MARK = "";

/** The fence language {@link prepareMath} gives `$$…$$` and `\[…\]`: plain display math, no toggle. */
export const DISPLAY_MATH_LANG = "skynet-display-math";

const FENCE = /^ {0,3}(`{3,}|~{3,})/;

/** Split text into runs outside and inside fenced code blocks. */
function splitFences(text: string): Array<{ code: boolean; text: string; open?: boolean }> {
  const parts: Array<{ code: boolean; text: string; open?: boolean }> = [];
  let open: string | null = null;
  let buf = "";
  const flush = (code: boolean) => {
    if (buf) parts.push({ code, text: buf });
    buf = "";
  };
  for (const line of text.split(/(?<=\n)/)) {
    const fence = FENCE.exec(line)?.[1];
    if (open === null && fence) {
      flush(false);
      open = fence;
      buf = line;
    } else if (
      open !== null &&
      fence &&
      fence[0] === open[0] &&
      fence.length >= open.length &&
      line.trim() === fence
    ) {
      buf += line;
      flush(true);
      open = null;
    } else {
      buf += line;
    }
  }
  if (open !== null && buf) parts.push({ code: true, text: buf, open: true });
  else flush(false);
  return parts;
}

// Code spans come first in the alternation so math inside them stays code.
const MATH = /(`+)[\s\S]*?\1|\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)/g;

/**
 * Turn `$$…$$`, `\[…\]` and `\(…\)` into nodes the renderer draws as math:
 * a display formula on its own lines becomes a `math` fence, any other one
 * an inline code span with {@link INLINE_MATH_MARK}. A single `$` stays
 * text, because chat replies use it for money.
 */
export function prepareMath(text: string): string {
  if (!/\$\$|\\[[(]/.test(text)) return text;
  return splitFences(text)
    .map((part) =>
      part.code
        ? part.text
        : part.text.replace(
            MATH,
            (match, ticks, dollars, bracket, paren, offset: number, all: string) => {
              if (ticks) return match;
              const tex = String(dollars ?? bracket ?? paren).trim();
              if (!tex) return match;
              const before = all.slice(0, offset);
              const after = all.slice(offset + match.length);
              const ownLines = /(^|\n)[ \t]*$/.test(before) && /^[ \t]*(\n|$)/.test(after);
              if (paren === undefined && ownLines)
                return `\`\`\`${DISPLAY_MATH_LANG}\n${tex}\n\`\`\``;
              return `\`\` ${INLINE_MATH_MARK}${tex.replace(/\s+/g, " ")} \`\``;
            },
          ),
    )
    .join("");
}

/**
 * Fix the end of a reply that is still streaming, so half-typed syntax does
 * not flash: drop a link or image whose `[` has no full target yet, and
 * close a dangling inline code span, `*` or `**`. An open code fence stays
 * as it is: the parser already reads it as a code block to the end.
 */
export function repairStreamingMarkdown(text: string): string {
  const last = splitFences(text).at(-1);
  if (!last || last.code) return text;
  const prose = last.text;
  const blank = prose.search(/\n[ \t]*\n(?![\s\S]*\n[ \t]*\n)/);
  const head = text.slice(0, text.length - prose.length) + (blank < 0 ? "" : prose.slice(0, blank));
  let tail = blank < 0 ? prose : prose.slice(blank);

  tail = tail.replace(/!?\[[^\]\n]*(\]\([^)\n]*)?$/, "");
  // Close the code span first, so a `*` inside it does not count as emphasis below.
  if (tail.replace(/`[^`]*`/g, "").includes("`"))
    tail = /`\s*$/.test(tail) ? tail.replace(/`\s*$/, "") : `${tail}\``;
  const plain = tail
    .replace(/`[^`\n]*`/g, "")
    .replace(/^[ \t]*[*+-][ \t]+/gm, "")
    .replace(/^[ \t]*(\*[ \t]*){3,}$/gm, "");
  const strong = (plain.match(/\*\*/g) ?? []).length;
  const single = (plain.replace(/\*\*/g, "").match(/\*/g) ?? []).length;
  // A closer after a space does not close, so the tail's end space goes first.
  if (strong % 2)
    tail = /\*\*\s*$/.test(tail) ? tail.replace(/\*\*\s*$/, "") : `${tail.trimEnd()}**`;
  if (single % 2)
    tail = /(^|[^*])\*\s*$/.test(tail) ? tail.replace(/\*\s*$/, "") : `${tail.trimEnd()}*`;
  return head + tail;
}

/** Whether the text ends inside a code fence that is not closed yet. */
export function endsInOpenFence(text: string): boolean {
  return splitFences(text).at(-1)?.open === true;
}

// Display names for fence languages, as the Codex app shows them in a code block header.
const LANGUAGE_NAMES: Record<string, string> = {
  arduino: "Arduino",
  bash: "Bash",
  c: "C",
  cpp: "C++",
  csharp: "C#",
  css: "CSS",
  diff: "Diff",
  go: "Go",
  graphql: "GraphQL",
  http: "HTTP",
  ini: "INI",
  java: "Java",
  javascript: "JavaScript",
  json: "JSON",
  kotlin: "Kotlin",
  latex: "LaTeX",
  less: "Less",
  lua: "Lua",
  makefile: "Makefile",
  math: "Math",
  markdown: "Markdown",
  mermaid: "Mermaid",
  objectivec: "Objective-C",
  perl: "Perl",
  php: "PHP",
  "php-template": "PHP template",
  plaintext: "",
  python: "Python",
  "python-repl": "Python REPL",
  r: "R",
  ruby: "Ruby",
  rust: "Rust",
  scss: "SCSS",
  shell: "Shell",
  sql: "SQL",
  svg: "SVG",
  swift: "Swift",
  typescript: "TypeScript",
  vbnet: "Visual Basic .NET",
  wasm: "WebAssembly",
  xml: "XML",
  yaml: "YAML",
};

const LANGUAGE_ALIASES: Record<string, string> = {
  "c#": "csharp",
  "c++": "cpp",
  cs: "csharp",
  gql: "graphql",
  js: "javascript",
  jsx: "javascript",
  md: "markdown",
  objc: "objectivec",
  patch: "diff",
  py: "python",
  rb: "ruby",
  rs: "rust",
  sh: "bash",
  tex: "latex",
  text: "plaintext",
  ts: "typescript",
  tsx: "typescript",
  vb: "vbnet",
  yml: "yaml",
  zsh: "bash",
};

/**
 * The title a code block shows: a known language's display name, else the
 * fence's first word. "" means plain text or no language, so the caller
 * shows its own localized title.
 */
export function codeBlockLabel(language: string): string {
  const key = language.trim().toLowerCase();
  if (key === "html") return "HTML";
  const name = LANGUAGE_NAMES[LANGUAGE_ALIASES[key] ?? key];
  return name ?? language;
}

const DATA_IMAGE = /^data:image\/(png|jpeg|jpg|webp|gif|avif);base64,/i;

/** Whether an image source is an inline raster image, the only kind chat loads by itself. */
export function isInlineImageSrc(src: string): boolean {
  return DATA_IMAGE.test(src);
}

const MATH_FENCES = new Set(["latex", "math", "tex"]);

/** Whether a fence holds TeX that renders as display math with a source toggle. */
export function isMathFence(language: string): boolean {
  return MATH_FENCES.has(language.toLowerCase());
}

/** Whether a fence holds an SVG image: an `svg` fence, or an `xml` or `html` one that starts with `<svg`. */
export function isSvgFence(language: string, code: string): boolean {
  const key = language.toLowerCase();
  if (key === "svg") return true;
  return (key === "xml" || key === "html") && /^<svg[\s>]/i.test(code.trimStart());
}

/** The SVG as a data URL for an `<img>`, where its scripts and links never run. */
export function svgDataUrl(code: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(code.trim())}`;
}

// Each rule needs a strong sign of its language, so prose in an untagged fence stays plain.
const GUESSES: Array<[string, (code: string) => boolean]> = [
  ["diff", (c) => /^(diff --git |@@ -\d)/m.test(c) || /^--- .*\n\+\+\+ /m.test(c)],
  [
    "json",
    (c) => {
      if (!/^[[{]/.test(c)) return false;
      try {
        JSON.parse(c);
        return true;
      } catch {
        return false;
      }
    },
  ],
  ["xml", (c) => /^<\?xml\b|^<svg[\s>]/i.test(c)],
  [
    "html",
    (c) =>
      /^<(!doctype html|html|head|body|div|span|p|a|ul|ol|table|section|main|form)[\s>]/i.test(c),
  ],
  [
    "bash",
    (c) =>
      /^(\$ |#!.*\b(ba|z)?sh\b|(sudo|npm|npx|pnpm|yarn|pip3?|uv|git|cd|ls|curl|wget|brew|docker|kubectl|export|echo|mkdir|chmod|cargo|just)( |$))/.test(
        c,
      ),
  ],
  [
    "python",
    (c) =>
      /^\s*(def \w+\(.*\)( -> .+)?:|class \w+(\(.*\))?:|from [\w.]+ import |import \w+(\.\w+)*\s*$|if __name__ ==|elif .+:)/m.test(
        c,
      ),
  ],
  [
    "typescript",
    (c) =>
      /^\s*(export )?(interface \w+|type \w+(<.*>)? =|enum \w+ \{)/m.test(c) ||
      /\b(const|let|function)\b[^\n]*: (string|number|boolean|unknown)\b/.test(c),
  ],
  [
    "javascript",
    (c) =>
      /^\s*(import .+ from ['"]|export (default|const|function)|(const|let|var) \w+ = |function \w*\(|console\.log\()/m.test(
        c,
      ),
  ],
  ["go", (c) => /^package \w+$/m.test(c) || /^func (\(.+\) )?\w+\(/m.test(c)],
  ["rust", (c) => /^\s*(pub )?fn \w+(<.*>)?\(/m.test(c) || /\blet mut \w+/.test(c)],
  [
    "sql",
    (c) =>
      /^\s*(SELECT\b[\s\S]*\bFROM\b|INSERT INTO\b|UPDATE \w+ SET\b|DELETE FROM\b|CREATE (TABLE|INDEX|VIEW)\b)/i.test(
        c,
      ),
  ],
  ["css", (c) => /^[.#@:\w][^{}\n;=()]*\{\s*[\w-]+\s*:[^;{}]+;/.test(c)],
  [
    "yaml",
    (c) => {
      const lines = c.split("\n").filter((l) => l.trim() && !/^\s*#/.test(l));
      return (
        lines.length >= 2 &&
        /^[\w.-]+:( |$)/.test(lines[0]!) &&
        lines.every((l) => /^\s*(- .*|-$|[\w.-]+:( .*)?)$/.test(l))
      );
    },
  ],
];

/**
 * Guess the language of a fence that has no tag, as Codex does with
 * highlight.js `highlightAuto`. "" means no strong sign, so the block stays plain.
 *
 * Args:
 *   code: The fence's text.
 *
 * Returns:
 *   A fence language name, such as "python", or "".
 */
export function guessLanguage(code: string): string {
  const text = code.trim();
  if (!text) return "";
  return GUESSES.find(([, test]) => test(text))?.[0] ?? "";
}
