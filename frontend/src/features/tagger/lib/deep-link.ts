/**
 * Whether a /tagger URL skips the session chooser and opens the setup.
 * `?dataset=<id>` comes from "Tag this dataset" and from the chat; `?new=1`
 * comes from the chat when no saved dataset fits (SKY-631).
 */
export function opensTaggerSetup(params: { has(name: string): boolean }): boolean {
  return params.has("dataset") || params.has("new");
}

/**
 * Whether the tagger shows its setup after the URL's deep link changes from
 * `previous` to `next` (each a dataset id, "" for ?new=1, or null). A link opens
 * the setup. A link that goes away, as on browser Back to a bare /tagger, shows
 * the chooser again. With no link before or after, the user's choice stays.
 */
export function startingNewAfterLink(
  startingNew: boolean,
  previous: string | null,
  next: string | null,
): boolean {
  if (next !== null) return true;
  if (previous !== null) return false;
  return startingNew;
}

/**
 * The name of a linked library dataset. The chat's link carries only the id,
 * because a name can hold characters the safe link rule refuses, so the name
 * then comes from the caller's dataset list.
 */
export function linkedDatasetName(
  datasetId: string,
  linkName: string | null,
  datasets: ReadonlyArray<{ id: string; name: string }>,
): string | null {
  return linkName || datasets.find((d) => d.id === datasetId)?.name || null;
}
