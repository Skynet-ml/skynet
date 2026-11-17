/**
 * Long pastes and attached files ride inside the message string as tagged
 * blocks, so every chat keeps its plain `value: string` contract and the agent
 * still reads the full text, while the composer and the sent bubble show each
 * block as a card.
 */

/** Same cutoff ChatGPT uses before a paste becomes an attachment. */
const PASTE_ATTACHMENT_MIN_CHARS = 5000;

const BLOCK_RE =
  /<pasted_text>\n?([\s\S]*?)\n?<\/pasted_text>\n*|<attached_file name="([^"]*)"(?: id="([^"]*)")?>\n?([\s\S]*?)\n?<\/attached_file>\n*/g;

/**
 * A file attached to a message: its name, the text the agent reads, and the
 * card id that links it to its stored original. Messages sent before SKY-782
 * have no id.
 */
export interface AttachedFile {
  name: string;
  text: string;
  id?: string;
}

export interface SplitMessage {
  text: string;
  pastes: string[];
  files: AttachedFile[];
}

/** Separate a message into what was typed, its attached files and its pasted-text blocks. */
export function splitPastes(value: string): SplitMessage {
  const pastes: string[] = [];
  const files: AttachedFile[] = [];
  const text = value.replace(
    BLOCK_RE,
    (
      _,
      paste: string | undefined,
      name: string | undefined,
      id: string | undefined,
      body: string | undefined,
    ) => {
      if (paste !== undefined) pastes.push(paste);
      else files.push({ name: name ?? "", text: body ?? "", ...(id ? { id } : {}) });
      return "";
    },
  );
  const found = pastes.length + files.length > 0;
  // Drop only the one separator before the blocks: the composer re-splits on
  // every keystroke, so trimming more would eat a space the user just typed.
  return { text: found ? text.replace(/^\s+/, "").replace(/\n\n$/, "") : text, pastes, files };
}

/**
 * Rebuild the message string: typed text first, then files and pastes. The
 * backend cuts long history turns from the start, so the question must lead.
 */
export function joinPastes({ text, pastes, files }: SplitMessage): string {
  if (!pastes.length && !files.length) return text;
  const blocks = [
    ...files.map(
      (f) =>
        `<attached_file name="${f.name}"${f.id ? ` id="${f.id}"` : ""}>\n${f.text}\n</attached_file>`,
    ),
    ...pastes.map((p) => `<pasted_text>\n${p}\n</pasted_text>`),
  ].join("\n\n");
  return text ? `${text}\n\n${blocks}` : blocks;
}

/**
 * Build an attached file whose name and text cannot break out of its block.
 * Its new card id links it to its stored original, also after a reload.
 */
export function attachedFile(name: string, text: string): AttachedFile & { id: string } {
  return {
    name: name.replace(/["<>\r\n]/g, "_"),
    text: text.replaceAll("</attached_file>", "</attached_file >"),
    id: crypto.randomUUID(),
  };
}

/**
 * Put a workspace path at the top of each file card (SKY-781): ``paths`` holds
 * one path, or null, for each card in order. A card that a fork put back
 * names the source chat's copy, so its old path goes first.
 */
export function withFilePaths(value: string, paths: ReadonlyArray<string | null>): string {
  const draft = splitPastes(value);
  if (!draft.files.length) return value;
  const files = draft.files.map((file, i) => {
    const text = file.text.replace(/^path: .*\n/, "");
    const path = paths[i];
    // The card keeps its id, which links its stored original (SKY-782).
    return { ...file, text: path ? attachedFile(file.name, `path: ${path}\n${text}`).text : text };
  });
  return joinPastes({ ...draft, files });
}

/** Whether a paste is long enough to become a card instead of draft text. */
export function shouldAttachPaste(pasted: string): boolean {
  return pasted.length >= PASTE_ATTACHMENT_MIN_CHARS;
}

export function countLines(text: string): number {
  return text ? text.split("\n").length : 0;
}
