import type { MediaAttachment } from "@/shared/ui/agent/media-attachments";

// A `document` is the original of an attached file (SKY-735, SKY-782).
const KINDS = new Set(["image", "video", "audio", "document"]);

/**
 * Read the files a stored user turn names (SKY-707). A reload has no local
 * URL, so each file gets an empty one and plays from its stored id. Entries
 * without an id, a name or a known kind are dropped. A document keeps the id
 * of the file card it belongs to, when the turn stored one (SKY-782).
 */
export function parseStoredMedia(raw: unknown): MediaAttachment[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const media = raw.flatMap((entry): MediaAttachment[] => {
    if (!entry || typeof entry !== "object") return [];
    const { id, name, kind, card } = entry as Record<string, unknown>;
    if (typeof id !== "string" || typeof name !== "string" || typeof kind !== "string") return [];
    if (!KINDS.has(kind)) return [];
    return [
      {
        id,
        name,
        kind: kind as MediaAttachment["kind"],
        url: "",
        ...(kind === "document" && typeof card === "string" ? { card } : {}),
      },
    ];
  });
  return media.length ? media : undefined;
}
