import type { MediaAttachment } from "@/shared/ui/agent/media-attachments";

const KINDS = new Set(["image", "video", "audio"]);

/**
 * Read the files a stored user turn names (SKY-707). A reload has no local
 * URL, so each file gets an empty one and plays from its stored id. Entries
 * without an id, a name or a known kind are dropped.
 */
export function parseStoredMedia(raw: unknown): MediaAttachment[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const media = raw.flatMap((entry): MediaAttachment[] => {
    if (!entry || typeof entry !== "object") return [];
    const { id, name, kind } = entry as Record<string, unknown>;
    if (typeof id !== "string" || typeof name !== "string" || typeof kind !== "string") return [];
    if (!KINDS.has(kind)) return [];
    return [{ id, name, kind: kind as MediaAttachment["kind"], url: "" }];
  });
  return media.length ? media : undefined;
}
