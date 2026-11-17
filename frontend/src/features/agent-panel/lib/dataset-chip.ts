/**
 * A confirmed dataset waits in the composer draft as one file card, as a PDF
 * does, and goes with the user's message on Send (SKY-780).
 */

import { joinPastes, splitPastes, type AttachedFile } from "@/shared/ui/agent/pasted-text";

function sameFile(a: AttachedFile, b: AttachedFile): boolean {
  return a.name === b.name && a.text === b.text;
}

/** Put a dataset's card last in the draft, in place of the earlier dataset's card. */
export function putDatasetChip(
  draft: string,
  chip: AttachedFile,
  earlier: AttachedFile | null,
): string {
  const split = splitPastes(draft);
  const files = split.files.filter(
    (file) => !sameFile(file, chip) && !(earlier && sameFile(file, earlier)),
  );
  return joinPastes({ ...split, files: [...files, chip] });
}

/** Whether the draft still holds the card; the user can remove it before Send. */
export function draftHasChip(draft: string, chip: AttachedFile): boolean {
  return splitPastes(draft).files.some((file) => sameFile(file, chip));
}
