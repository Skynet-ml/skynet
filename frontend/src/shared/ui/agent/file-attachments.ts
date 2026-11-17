/**
 * Read files picked or pasted into an agent composer. Any file type is
 * accepted: the backend turns each one into the text the agent reads (or a
 * short note when it cannot), so the composer never filters by extension.
 */

/** Matches the backend's MAX_ATTACHMENT_BYTES; larger files are not uploaded. */
export const MAX_ATTACHMENT_MB = 20;
const MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024;

export interface ReadAttachments {
  /** Each read file's name and text, and the picked file itself for its original. */
  attached: Array<{ name: string; text: string; file: File }>;
  failed: string[];
  tooLarge: string[];
}

export interface ClaimedFiles {
  claimed: File | null;
  extra: File[];
  rest: File[];
}

/** Split picked files: the host takes the first file it claims, the other
 *  files it would claim are skipped, and the rest are read as text. */
export function splitClaimed(
  files: readonly File[],
  claims: (file: File) => boolean,
): ClaimedFiles {
  const result: ClaimedFiles = { claimed: null, extra: [], rest: [] };
  for (const file of files) {
    if (!claims(file)) result.rest.push(file);
    else if (result.claimed) result.extra.push(file);
    else result.claimed = file;
  }
  return result;
}

/** Read each file's text for the agent, keeping the order they were picked in.
 *  Files are read one at a time: the backend reads only a few documents at
 *  once and refuses the rest. */
export async function readAttachments(
  files: readonly File[],
  extract: (file: File) => Promise<string>,
): Promise<ReadAttachments> {
  const result: ReadAttachments = { attached: [], failed: [], tooLarge: [] };
  for (const file of files) {
    if (file.size > MAX_ATTACHMENT_BYTES) {
      result.tooLarge.push(file.name);
      continue;
    }
    try {
      result.attached.push({ name: file.name, text: await extract(file), file });
    } catch {
      result.failed.push(file.name);
    }
  }
  return result;
}
