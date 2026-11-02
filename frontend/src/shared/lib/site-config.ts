/**
 * Single source of truth for the public site URL used in metadata, OG tags,
 * the JSON-LD payload, robots.txt, and sitemap.xml.
 *
 * `NEXT_PUBLIC_SITE_URL` names the deployment's own origin (a preview
 * environment sets its own); the public `https://skynetml.com` is the fallback
 * when it is unset or empty. Centralising the lookup here keeps a future
 * "fail-closed when unset in production" tweak to one place.
 */
const FALLBACK_SITE_URL = "https://skynetml.com";

export function getSiteUrl(): string {
  // The Dockerfile copies an unset build arg into ENV as "", so treat empty as unset.
  // The Mac app build sets no NEXT_PUBLIC_SITE_URL; its server reads the build's
  // hosted origin (staging for a staging build) from APP_HOSTED_APP_URL at run time.
  return process.env.NEXT_PUBLIC_SITE_URL || process.env.APP_HOSTED_APP_URL || FALLBACK_SITE_URL;
}

const PRODUCTION_API_URL = "https://api.skynetml.com";
const PRODUCTION_MCP_URL = "https://mcp.skynetml.com/mcp";
const PREVIEW_HOST = /^pr-(\d+)\.staging\.skynetml\.com$/;

/** Public API and remote MCP URLs of the deployment at `siteUrl`. Unknown sites get production. */
export function serviceUrls(siteUrl: string = getSiteUrl()): { api: string; mcp: string } {
  let host = "";
  try {
    host = new URL(siteUrl).hostname;
  } catch {
    // An unparseable site URL names no known deployment.
  }
  if (host === "staging.skynetml.com") {
    return { api: "https://api-staging.skynetml.com", mcp: "https://mcp.staging.skynetml.com/mcp" };
  }
  const preview = PREVIEW_HOST.exec(host);
  if (preview) {
    // A preview's MCP server lives under its API host (infra/terraform/scripts/preview.sh).
    const api = `https://api-pr-${preview[1]}.staging.skynetml.com`;
    return { api, mcp: `${api}/remote-mcp` };
  }
  return { api: PRODUCTION_API_URL, mcp: PRODUCTION_MCP_URL };
}

/** Docs Markdown with the production API and MCP URLs replaced by those of `siteUrl`. */
export function withServiceUrls(markdown: string, siteUrl: string = getSiteUrl()): string {
  const { api, mcp } = serviceUrls(siteUrl);
  return markdown.split(PRODUCTION_MCP_URL).join(mcp).split(PRODUCTION_API_URL).join(api);
}
