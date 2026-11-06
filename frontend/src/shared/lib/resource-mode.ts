import type { ApiSource } from "@/shared/lib/api-route";

/** localStorage key the usage and storage pages share, so both show the same side. */
export const RESOURCE_MODE_KEY = "skynet.resource-mode";

/**
 * Which resources the settings pages show (SKY-340). A linked Mac shows the
 * web account (Remote) unless the user picked this Mac; an unlinked Mac has
 * only its own resources; the web app has only the web account.
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
