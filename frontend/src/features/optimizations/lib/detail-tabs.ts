/*
 * Which detail tab a run page opens on. Shared by the loaded view and its
 * loading skeleton so a deep link (`?tab=data`) draws the same tab in both.
 */

/** Phones get the view-first subset; Data, Code and Config are desk work. */
export const PHONE_DETAIL_TABS: ReadonlySet<string> = new Set([
  "overview",
  "best",
  "artifact",
  "logs",
  "usage",
]);

/** Every tab the run page can draw. */
const DETAIL_TABS: ReadonlySet<string> = new Set([
  "overview",
  "best",
  "code",
  "logs",
  "usage",
  "continuous",
  "config",
]);

/** The usage tab replaced these two; old links land on it. */
const RENAMED_TABS: Record<string, string> = { "lm-activity": "usage", budget: "usage" };

/** The tab a `?tab=` value asks for, before the phone shell narrows it; an unknown tab opens Overview. */
export function requestedDetailTab(requested: string | null): string {
  const tab = RENAMED_TABS[requested ?? ""] ?? requested ?? "overview";
  return DETAIL_TABS.has(tab) ? tab : "overview";
}

/** The tab actually shown: a deep link to a desk-only tab lands phones on Overview. */
export function shownDetailTab(tab: string, isPhone: boolean): string {
  return isPhone && !PHONE_DETAIL_TABS.has(tab) ? "overview" : tab;
}
