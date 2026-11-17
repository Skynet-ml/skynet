/**
 * The original bytes of an attached file (SKY-735). The agent reads a file's
 * text, which travels in the message as an `<attached_file>` block. Every file
 * also keeps its original as a stored `document` (SKY-782). The viewer draws a
 * PDF, DOCX or XLSX original; any other file shows its text page. A block's
 * card id links its original. A block sent before SKY-782 has no id, so its
 * original pairs with it by name, in order.
 */

/** The formats whose original the viewer draws. */
export type OriginalFormat = "pdf" | "docx" | "xlsx";

const FORMATS: readonly string[] = ["pdf", "docx", "xlsx"];
// The block and the server each clean a name in their own way. The server keeps
// only the part after the last slash or backslash. Then dropping every character
// that either one may change gives one key for both.
const CHANGED = new Set(['"', "<", ">", "_"]);

/** The format of a file whose original the viewer draws, from its name, or null. */
export function originalFormat(name: string): OriginalFormat | null {
  // The key decides, here and on the server (chat_media._draw_key), so the
  // raw name, the block's name and the stored name all give one answer.
  const key = nameKey(name);
  const dot = key.lastIndexOf(".");
  const ext = dot > 0 ? key.slice(dot + 1).toLowerCase() : "";
  return FORMATS.includes(ext) ? (ext as OriginalFormat) : null;
}

/** One key for a file name and its cleaned forms; the server types an original by the same key. */
export function nameKey(name: string): string {
  return Array.from(name.normalize("NFC").split(/[\\/]/).pop()!)
    .filter((c) => c > "\u001f" && c !== "\u007f" && !CHANGED.has(c))
    .join("")
    .trim();
}

/**
 * The original of each file of a message sent before SKY-782, in the files'
 * order: the first unpaired original with the same name, or null. A file in a format the viewer does not draw
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

/**
 * The originals that fit in ``room`` places of a message, in their order. The
 * viewer draws only a PDF, DOCX or XLSX original, so those fit first.
 */
export function fitOriginals<T extends { name: string }>(
  originals: readonly T[],
  room: number,
): T[] {
  const drawn = originals.filter((o) => originalFormat(o.name));
  const rest = originals.filter((o) => !originalFormat(o.name));
  const fits = new Set([...drawn, ...rest].slice(0, Math.max(0, room)));
  return originals.filter((o) => fits.has(o));
}
