import { APP_SCHEME } from "./app-url";

export type Theme = "light" | "dark";

/** The theme in an IPC message from the web app, or null for any other value. */
export function parseTheme(value: unknown): Theme | null {
  return value === "light" || value === "dark" ? value : null;
}

/** A theme the web app reported wins over the system appearance. */
export function pickTheme(reported: Theme | null, systemDark: boolean): Theme {
  return reported ?? (systemDark ? "dark" : "light");
}

/** True for skynet://open, which only asks to show the app window. */
export function isOpenAppUrl(raw: string): boolean {
  try {
    const url = new URL(raw);
    return url.protocol === `${APP_SCHEME}:` && url.host === "open" && (url.pathname === "" || url.pathname === "/");
  } catch {
    return false;
  }
}
