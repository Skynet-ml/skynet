import type { ApiSource } from "@/shared/lib/api-route";

/** localStorage key of the Mac app's Local/Web pick. */
export const RESOURCE_MODE_KEY = "skynet.resource-mode";

/** Window event sent when the Local/Web pick changes, so every screen follows it. */
export const RESOURCE_MODE_EVENT = "skynet:resource-mode";

// Storage can be off; the pick then holds in memory until the app reloads.
let memoryPick: string | null = null;

/** The stored Local/Web pick, or null before the first pick. */
export function readResourcePick(): string | null {
  try {
    return window.localStorage.getItem(RESOURCE_MODE_KEY) ?? memoryPick;
  } catch {
    return memoryPick;
  }
}

/** Store the Local/Web pick and tell every screen. */
export function writeResourcePick(pick: ApiSource): void {
  memoryPick = pick;
  try {
    window.localStorage.setItem(RESOURCE_MODE_KEY, pick);
  } catch {
    // Storage off: memoryPick keeps the pick for this session.
  }
  window.dispatchEvent(new Event(RESOURCE_MODE_EVENT));
}

/**
 * Which side the app works on (SKY-340). A linked Mac uses the web account
 * unless the user picked this Mac; an unlinked Mac has only its own side;
 * the web app has only the web account.
 */
export function resolveResourceMode(
  stored: string | null,
  localBuild: boolean,
  linked: boolean,
): ApiSource {
  if (!localBuild) return "hosted";
  if (!linked) return "local";
  return stored === "local" ? "local" : "hosted";
}
