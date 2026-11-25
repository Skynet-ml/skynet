/**
 * Attach files picked or pasted into an agent composer. Any file type is
 * accepted. The message holds no file text, only the file's facts; the agent
 * reads the stored file itself with shell commands (SKY-781), so the composer
 * never filters by extension.
 */

/** Matches the backend's MAX_ATTACHMENT_BYTES; larger files are not uploaded. */
export const MAX_ATTACHMENT_MB = 20;
const MAX_ATTACHMENT_BYTES = MAX_ATTACHMENT_MB * 1024 * 1024;

export interface ReadAttachments {
  /** Each file's name and card text, and the picked file itself for its original. */
  attached: Array<{ name: string; text: string; file: File }>;
  tooLarge: string[];
}

export interface ClaimedFiles {
  claimed: File | null;
  extra: File[];
  rest: File[];
}

/** Split picked files: the host takes the first file it claims, the other
 *  files it would claim are skipped, and the rest are attached. */
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

/** Give each file's type and size for its card, in the order picked. */
export function describeAttachments(files: readonly File[]): ReadAttachments {
  const result: ReadAttachments = { attached: [], tooLarge: [] };
  for (const file of files) {
    if (file.size > MAX_ATTACHMENT_BYTES) result.tooLarge.push(file.name);
    else
      result.attached.push({
        name: file.name,
        text: `type: ${file.type || "unknown"}\nsize: ${file.size} bytes`,
        file,
      });
  }
  return result;
}
