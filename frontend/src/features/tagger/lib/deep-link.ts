/**
 * Whether a /tagger URL skips the session chooser and opens the setup.
 * `?dataset=<id>` comes from "Tag this dataset" and from the chat; `?new=1`
 * comes from the chat when no saved dataset fits (SKY-631).
 */
export function opensTaggerSetup(params: { has(name: string): boolean }): boolean {
  return params.has("dataset") || params.has("new");
}
