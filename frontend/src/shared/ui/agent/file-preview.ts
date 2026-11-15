/**
 * Pick how an attached file's text is shown, from its name and the text the
 * backend extracted (`service_gateway/agents/attachments.py`). A file the
 * backend could not read gets no preview, so its tile is not clickable.
 */

/** One table: a sheet of a workbook, or a whole CSV or TSV file. */
export interface PreviewTable {
  /** The sheet name; "" for a CSV or TSV file. */
  name: string;
  rows: string[][];
}

export type FilePreview =
  | { kind: "code"; language: string; text: string; note: string }
  | { kind: "markdown"; text: string; note: string }
  | { kind: "plain"; text: string; note: string }
  | { kind: "tables"; tables: PreviewTable[]; note: string };

/** More rows than this render as plain text: a table that big would stall the page. */
export const MAX_TABLE_ROWS = 2000;

// The backend's stand-in for a file it could not read: one bracketed line.
const UNREADABLE_RE = /^\[Attached file [^\n]*\]$/;
// The backend ends a cut file with one bracketed "[Truncated: …]" line.
const TRUNCATED_RE = /\n*(\[Truncated: [^\n]*\])$/;
const MARKDOWN_EXTS = new Set(["md", "markdown", "mdx"]);
const SHEET_EXTS = new Set(["xlsx", "xlsm", "xls"]);

/** Split CSV text into rows of cells; quoted cells may hold commas, quotes and newlines. */
export function parseCsv(text: string, sep = ","): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        cell += '"';
        i++;
      } else if (c === '"') quoted = false;
      else cell += c;
    } else if (c === '"' && cell === "") quoted = true;
    else if (c === sep) {
      row.push(cell);
      cell = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else cell += c;
  }
  if (cell !== "" || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

/** Split the backend's workbook text ("Sheet: name" then tab-separated rows) into tables. */
export function parseSheets(text: string): PreviewTable[] {
  return text
    .split(/(?:^|\n\n)Sheet: /)
    .slice(1)
    .map((part) => {
      const nl = part.indexOf("\n");
      const name = nl < 0 ? part : part.slice(0, nl);
      const body = nl < 0 ? "" : part.slice(nl + 1);
      return { name, rows: body ? body.split("\n").map((line) => line.split("\t")) : [] };
    });
}

/**
 * How to show an attached file, or null when it has nothing worth showing.
 *
 * @param ext - The file's lower-case extension.
 * @param isCode - Whether the extension names a source code file.
 */
export function filePreview(ext: string, isCode: boolean, text: string): FilePreview | null {
  const trimmed = text.trim();
  if (!trimmed || UNREADABLE_RE.test(trimmed)) return null;
  const cut = TRUNCATED_RE.exec(trimmed);
  const note = cut?.[1] ?? "";
  const body = cut ? trimmed.slice(0, cut.index) : text;
  const tables =
    ext === "csv" || ext === "tsv"
      ? [{ name: "", rows: parseCsv(body, ext === "tsv" ? "\t" : ",") }]
      : SHEET_EXTS.has(ext) && body.startsWith("Sheet: ")
        ? parseSheets(body)
        : null;
  if (tables) {
    const count = tables.reduce((n, t) => n + t.rows.length, 0);
    if (count > 0 && count <= MAX_TABLE_ROWS) return { kind: "tables", tables, note };
    return { kind: "plain", text: body, note };
  }
  if (MARKDOWN_EXTS.has(ext)) return { kind: "markdown", text: body, note };
  if (isCode) return { kind: "code", language: ext, text: body, note };
  return { kind: "plain", text: body, note };
}

/** Wrap code in a Markdown fence longer than any backtick run inside it. */
export function fenceCode(language: string, code: string): string {
  const longest = Math.max(2, ...(code.match(/`+/g) ?? []).map((run) => run.length));
  const fence = "`".repeat(longest + 1);
  return `${fence}${language}\n${code}\n${fence}`;
}
