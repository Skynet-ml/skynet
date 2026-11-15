/**
 * The original bytes of an attached file (SKY-735). The agent reads a file's
 * text, which travels in the message as an `<attached_file>` block. A PDF,
 * DOCX or XLSX also keeps its original as a stored `document`, so the viewer
 * can show the real file. The message names a file only by its name, so an
 * original pairs with its block by name, in order.
 */

/** The formats whose original the chat keeps and the viewer draws. */
export type OriginalFormat = "pdf" | "docx" | "xlsx";

const FORMATS: readonly string[] = ["pdf", "docx", "xlsx"];
// The block and the server each clean a name in their own way. Dropping every
// character that either one may change gives one key for both.
const CHANGED = new Set(['"', "\\", "/", "<", ">", "_"]);

/** The format of a file whose original the chat keeps, from its name, or null. */
export function originalFormat(name: string): OriginalFormat | null {
  const dot = name.lastIndexOf(".");
  const ext = dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
  return FORMATS.includes(ext) ? (ext as OriginalFormat) : null;
}

function nameKey(name: string): string {
  return Array.from(name.normalize("NFC"))
    .filter((c) => c > "\u001f" && c !== "\u007f" && !CHANGED.has(c))
    .join("")
    .trim();
}

/**
 * The original of each file, in the files' order: the first unpaired original
 * with the same name, or null. A file in a format the viewer does not draw
 * gets no original.
 */
export function pairOriginals<T extends { name: string }>(
  files: ReadonlyArray<{ name: string }>,
  originals: readonly T[],
): Array<T | null> {
  const used = new Set<number>();
  return files.map((file) => {
    if (!originalFormat(file.name)) return null;
    const key = nameKey(file.name);
    const i = originals.findIndex((o, j) => !used.has(j) && nameKey(o.name) === key);
    if (i < 0) return null;
    used.add(i);
    return originals[i] ?? null;
  });
}

/** The originals that still pair with a file, so a removed file's original does not go out. */
export function keptOriginals<T extends { name: string }>(
  files: ReadonlyArray<{ name: string }>,
  originals: readonly T[],
): T[] {
  return pairOriginals(files, originals).filter((o): o is T => o !== null);
}
