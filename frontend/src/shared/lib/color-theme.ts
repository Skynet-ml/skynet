/**
 * Light / dark color theme: the stored choice, its resolution against the
 * OS preference, and the pre-paint script that applies it.
 *
 * The choice lives per browser in localStorage. `system` (the default) follows
 * `prefers-color-scheme` live. The resolved theme is a `.dark` class plus the
 * matching `color-scheme` on `<html>`; every color token switches off that class.
 */

export const COLOR_THEMES = ["system", "light", "dark"] as const;
export type ColorTheme = (typeof COLOR_THEMES)[number];
export type ResolvedColorTheme = Exclude<ColorTheme, "system">;

export const COLOR_THEME_STORAGE_KEY = "app.prefs.color-theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";
// The former Gray theme became Dark; a choice saved under the old name keeps it.
const LEGACY_DARK = "gray";

export function isColorTheme(value: unknown): value is ColorTheme {
  return typeof value === "string" && (COLOR_THEMES as readonly string[]).includes(value);
}

/** Read the stored choice; storage can be absent or throw (private mode). */
export function readColorTheme(): ColorTheme {
  try {
    const raw = window.localStorage.getItem(COLOR_THEME_STORAGE_KEY);
    if (raw === LEGACY_DARK) return "dark";
    return isColorTheme(raw) ? raw : "system";
  } catch {
    return "system";
  }
}

export function writeColorTheme(theme: ColorTheme): void {
  try {
    if (theme === "system") window.localStorage.removeItem(COLOR_THEME_STORAGE_KEY);
    else window.localStorage.setItem(COLOR_THEME_STORAGE_KEY, theme);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
}

export function systemPrefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia(DARK_QUERY).matches;
}

export function resolveColorTheme(theme: ColorTheme): ResolvedColorTheme {
  if (theme === "system") return systemPrefersDark() ? "dark" : "light";
  return theme;
}

export function applyResolvedColorTheme(resolved: ResolvedColorTheme): void {
  const root = document.documentElement;
  root.classList.toggle("dark", resolved === "dark");
  root.style.colorScheme = resolved === "dark" ? "dark" : "light";
  // The tab icon follows the painted theme, not the OS, so a Dark choice on a
  // light OS still gets the dark tile.
  const href = resolved === "dark" ? "/favicon-dark.png?v=10" : "/favicon.png?v=10";
  document.querySelectorAll<HTMLLinkElement>('link[rel~="icon"]').forEach((link) => {
    if (link.getAttribute("href") !== href) link.href = href;
  });
}

export function watchSystemColorScheme(onChange: () => void): () => void {
  const query = window.matchMedia(DARK_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

/**
 * Inline `<head>` script: applies the stored theme before first paint so a
 * dark-mode visitor never sees a light flash. Mirrors readColorTheme +
 * resolveColorTheme + applyResolvedColorTheme with no imports.
 */
export const COLOR_THEME_INIT_SCRIPT = `(function(){try{var t=null;try{t=localStorage.getItem(${JSON.stringify(
  COLOR_THEME_STORAGE_KEY,
)})}catch(e){}var d=t==="dark"||t===${JSON.stringify(
  LEGACY_DARK,
)}||(t!=="light"&&window.matchMedia(${JSON.stringify(
  DARK_QUERY,
)}).matches);var r=document.documentElement;if(d)r.classList.add("dark");r.style.colorScheme=d?"dark":"light"}catch(e){}})();`;
