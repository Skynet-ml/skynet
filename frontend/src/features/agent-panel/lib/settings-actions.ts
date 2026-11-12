/**
 * Browser-side effects of the assistant's settings tools: `open_page` opens a
 * settings tab or an in-app route, and an `appearance` change from
 * `change_setting` comes back as a theme and language patch that only this
 * browser can apply. The caller checks the tab, theme and locale against the
 * app's own lists.
 */

export type AgentNavigation =
  | { kind: "settings"; tab: string }
  | { kind: "route"; path: string };

export interface AppearancePatch {
  theme?: string;
  locale?: string;
}

// Same rule as the backend: one leading slash, no scheme, host or "..". Only
// /tagger takes a query string, and it holds only ids and flags (SKY-631:
// /tagger?dataset=<id>). Other routes keep no query, because the screen agent
// matches a setup route by its exact path. The query has no "/", ":", "@",
// backslash, "#" or "%", so a value cannot hold a URL. The whole path, query
// included, is at most MAX_PATH characters.
const SAFE_PATH = /^(?:\/tagger\?[A-Za-z0-9_=&.-]{1,200}|\/(?!\/)[A-Za-z0-9/_-]{0,200})$/;
const MAX_PATH = 200;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Tool results arrive as objects or as JSON text; read either. */
function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value === "string") {
    try {
      return asRecord(JSON.parse(value));
    } catch {
      return null;
    }
  }
  return isRecord(value) ? value : null;
}

/** Read the navigation an `open_page` result asks for. */
export function parseAgentNavigation(result: unknown): AgentNavigation | null {
  const nav = asRecord(result)?.navigate;
  if (!isRecord(nav)) return null;
  if (nav.kind === "settings" && typeof nav.tab === "string" && nav.tab) {
    return { kind: "settings", tab: nav.tab };
  }
  if (
    nav.kind === "route" &&
    typeof nav.path === "string" &&
    nav.path.length <= MAX_PATH &&
    SAFE_PATH.test(nav.path)
  ) {
    return { kind: "route", path: nav.path };
  }
  return null;
}

/** Read the theme and language a `change_setting` result asks the browser to apply. */
export function parseAppearancePatch(result: unknown): AppearancePatch {
  const patch = asRecord(result)?.client_patch;
  if (!isRecord(patch)) return {};
  const out: AppearancePatch = {};
  if (typeof patch.theme === "string") out.theme = patch.theme;
  if (typeof patch.locale === "string") out.locale = patch.locale;
  return out;
}
