/**
 * The original bytes of an attached file (SKY-735). The agent reads a file's
 * text, which travels in the message as an `<attached_file>` block. Every file
 * also keeps its original as a stored `document` (SKY-782). The viewer draws a
 * PDF, DOCX or XLSX original; any other file shows its text page. The message
 * names a file only by its name, so an original pairs with its block by name,
 * in order.
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

/** The read files whose originals the chat keeps: a skipped file sends no text, so no original. */
export function originalsToKeep<T extends { name: string }>(
  files: readonly T[],
  skipped: Iterable<string>,
): T[] {
  const names = new Set(skipped);
  return files.filter((f) => !names.has(f.name));
}

/** One key for a file name and its cleaned forms; the server types an original by the same key. */
export function nameKey(name: string): string {
  return Array.from(name.normalize("NFC").split(/[\\/]/).pop()!)
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

/**
 * For each name key, the places among the key's files of the files at the
 * ``leaving`` indexes. The originals at those places pair with them (pairOriginals).
 */
export function pairedPlaces(
  files: ReadonlyArray<{ name: string }>,
  leaving: ReadonlySet<number>,
): Map<string, number[]> {
  const seen = new Map<string, number>();
  const places = new Map<string, number[]>();
  files.forEach((file, i) => {
    const key = nameKey(file.name);
    const place = seen.get(key) ?? 0;
    seen.set(key, place + 1);
    if (leaving.has(i)) places.set(key, [...(places.get(key) ?? []), place]);
  });
  return places;
}

/**
 * The originals that can go out with ``files``. Pairing by name is sure only
 * when a name has one original for each of its files. When the counts differ
 * (a file left, or an upload failed), no one can tell which file lost its
 * original, so all the files with that name fall back to their text.
 */
export function keptOriginals<T extends { name: string }>(
  files: ReadonlyArray<{ name: string }>,
  originals: readonly T[],
): T[] {
  const count = new Map<string, number>();
  for (const file of files) {
    const key = nameKey(file.name);
    count.set(key, (count.get(key) ?? 0) + 1);
  }
  for (const o of originals) {
    const key = nameKey(o.name);
    count.set(key, (count.get(key) ?? 0) - 1);
  }
  return originals.filter((o) => count.get(nameKey(o.name)) === 0);
}

/**
 * The originals that fit in ``room`` places of a message, the ones the viewer
 * draws first. A name's originals go together or not at all: half a group would leave its counts unequal, so
 * keptOriginals would drop the half that went too.
 */
export function fitOriginals<T extends { name: string }>(
  originals: readonly T[],
  room: number,
): T[] {
  const groups = new Map<string, number>();
  for (const o of originals) groups.set(nameKey(o.name), (groups.get(nameKey(o.name)) ?? 0) + 1);
  const fits = new Set<string>();
  let left = room;
  // The viewer draws only a PDF, DOCX or XLSX original, so those fit first.
  const drawn = [...groups].sort(
    ([a], [b]) => Number(!originalFormat(a)) - Number(!originalFormat(b)),
  );
  for (const [key, size] of drawn)
    if (size <= left) {
      fits.add(key);
      left -= size;
    }
  return originals.filter((o) => fits.has(nameKey(o.name)));
}
