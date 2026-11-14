/**
 * How code looks: a theme for light and dark mode, the code font, its size and
 * the diff markers. Settings > Code edits it; code views read it.
 *
 * Stored in the browser only, like the Claude and Codex desktop apps do.
 */

import * as React from "react";

/** The token colors one theme gives the `--code-*` variables, plus its background. */
export type CodePalette = {
  bg: string;
  text: string;
  keyword: string;
  string: string;
  comment: string;
  number: string;
  fn: string;
  type: string;
  punct: string;
};

type Theme = { id: string; name: string; palette: CodePalette };

// Theme names are proper nouns, so they stay untranslated.
const SKYNET_LIGHT: CodePalette = {
  bg: "#f6f2ec",
  text: "#1c1612",
  keyword: "#b4532f",
  string: "#3f7a3a",
  comment: "#88898d",
  number: "#a85a1c",
  fn: "#2f5f8a",
  type: "#8a6a12",
  punct: "#78797c",
};
const SKYNET_DARK: CodePalette = {
  bg: "#221d19",
  text: "#f3ede6",
  keyword: "#e39b7b",
  string: "#a9c48d",
  comment: "#8b8c92",
  number: "#e6a86e",
  fn: "#8fb8de",
  type: "#dcbc72",
  punct: "#a0a1a6",
};

const p = (
  bg: string,
  text: string,
  keyword: string,
  string: string,
  comment: string,
  number: string,
  fn: string,
  type: string,
  punct: string,
): CodePalette => ({ bg, text, keyword, string, comment, number, fn, type, punct });

export const LIGHT_THEMES: Theme[] = [
  { id: "skynet", name: "Skynet Light", palette: SKYNET_LIGHT },
  {
    id: "github",
    name: "GitHub Light",
    palette: p(
      "#ffffff",
      "#1f2328",
      "#cf222e",
      "#0a3069",
      "#6e7781",
      "#0550ae",
      "#8250df",
      "#953800",
      "#1f2328",
    ),
  },
  {
    id: "one",
    name: "One Light",
    palette: p(
      "#fafafa",
      "#383a42",
      "#a626a4",
      "#50a14f",
      "#a0a1a7",
      "#986801",
      "#4078f2",
      "#c18401",
      "#383a42",
    ),
  },
  {
    id: "solarized",
    name: "Solarized Light",
    palette: p(
      "#fdf6e3",
      "#657b83",
      "#859900",
      "#2aa198",
      "#93a1a1",
      "#d33682",
      "#268bd2",
      "#b58900",
      "#657b83",
    ),
  },
  {
    id: "catppuccin",
    name: "Catppuccin Latte",
    palette: p(
      "#eff1f5",
      "#4c4f69",
      "#8839ef",
      "#40a02b",
      "#9ca0b0",
      "#fe640b",
      "#1e66f5",
      "#df8e1d",
      "#7c7f93",
    ),
  },
  {
    id: "rose-pine",
    name: "Rosé Pine Dawn",
    palette: p(
      "#faf4ed",
      "#575279",
      "#286983",
      "#ea9d34",
      "#9893a5",
      "#907aa9",
      "#d7827e",
      "#56949f",
      "#797593",
    ),
  },
  {
    id: "vscode",
    name: "VS Code Light",
    palette: p(
      "#ffffff",
      "#000000",
      "#0000ff",
      "#a31515",
      "#008000",
      "#098658",
      "#795e26",
      "#267f99",
      "#000000",
    ),
  },
  {
    id: "xcode",
    name: "Xcode",
    palette: p(
      "#ffffff",
      "#000000",
      "#9b2393",
      "#c41a16",
      "#5d6c79",
      "#1c00cf",
      "#326d74",
      "#0b4f79",
      "#000000",
    ),
  },
  {
    id: "vitesse",
    name: "Vitesse Light",
    palette: p(
      "#ffffff",
      "#393a34",
      "#1e754f",
      "#b56959",
      "#a0ada0",
      "#2f798a",
      "#59873a",
      "#2e8f82",
      "#999999",
    ),
  },
  {
    id: "min",
    name: "Min Light",
    palette: p(
      "#ffffff",
      "#24292e",
      "#d32f2f",
      "#22863a",
      "#a0a1a7",
      "#1976d2",
      "#6f42c1",
      "#6f42c1",
      "#212121",
    ),
  },
  {
    id: "github-hc",
    name: "GitHub Light High Contrast",
    palette: p(
      "#ffffff",
      "#0e1116",
      "#a0111f",
      "#032563",
      "#66707b",
      "#023b95",
      "#622cbc",
      "#702c00",
      "#0e1116",
    ),
  },
  {
    id: "gruvbox",
    name: "Gruvbox Light",
    palette: p(
      "#fbf1c7",
      "#3c3836",
      "#9d0006",
      "#79740e",
      "#928374",
      "#8f3f71",
      "#427b58",
      "#b57614",
      "#3c3836",
    ),
  },
  {
    id: "everforest",
    name: "Everforest Light",
    palette: p(
      "#fdf6e3",
      "#5c6a72",
      "#f85552",
      "#8da101",
      "#939f91",
      "#df69ba",
      "#35a77c",
      "#dfa000",
      "#5c6a72",
    ),
  },
  {
    id: "ayu",
    name: "Ayu Light",
    palette: p(
      "#fcfcfc",
      "#5c6166",
      "#fa8d3e",
      "#86b300",
      "#adaeb1",
      "#a37acc",
      "#f2ae49",
      "#399ee6",
      "#5c6166",
    ),
  },
];

export const DARK_THEMES: Theme[] = [
  { id: "skynet", name: "Skynet Dark", palette: SKYNET_DARK },
  {
    id: "github",
    name: "GitHub Dark",
    palette: p(
      "#0d1117",
      "#e6edf3",
      "#ff7b72",
      "#a5d6ff",
      "#8b949e",
      "#79c0ff",
      "#d2a8ff",
      "#ffa657",
      "#e6edf3",
    ),
  },
  {
    id: "one",
    name: "One Dark Pro",
    palette: p(
      "#282c34",
      "#abb2bf",
      "#c678dd",
      "#98c379",
      "#5c6370",
      "#d19a66",
      "#61afef",
      "#e5c07b",
      "#abb2bf",
    ),
  },
  {
    id: "dracula",
    name: "Dracula",
    palette: p(
      "#282a36",
      "#f8f8f2",
      "#ff79c6",
      "#f1fa8c",
      "#6272a4",
      "#bd93f9",
      "#50fa7b",
      "#8be9fd",
      "#f8f8f2",
    ),
  },
  {
    id: "nord",
    name: "Nord",
    palette: p(
      "#2e3440",
      "#d8dee9",
      "#81a1c1",
      "#a3be8c",
      "#616e88",
      "#b48ead",
      "#88c0d0",
      "#8fbcbb",
      "#eceff4",
    ),
  },
  {
    id: "tokyo-night",
    name: "Tokyo Night",
    palette: p(
      "#1a1b26",
      "#a9b1d6",
      "#bb9af7",
      "#9ece6a",
      "#565f89",
      "#ff9e64",
      "#7aa2f7",
      "#2ac3de",
      "#89ddff",
    ),
  },
  {
    id: "night-owl",
    name: "Night Owl",
    palette: p(
      "#011627",
      "#d6deeb",
      "#c792ea",
      "#ecc48d",
      "#637777",
      "#f78c6c",
      "#82aaff",
      "#ffcb8b",
      "#7fdbca",
    ),
  },
  {
    id: "monokai",
    name: "Monokai",
    palette: p(
      "#272822",
      "#f8f8f2",
      "#f92672",
      "#e6db74",
      "#75715e",
      "#ae81ff",
      "#a6e22e",
      "#66d9ef",
      "#f8f8f2",
    ),
  },
  {
    id: "catppuccin",
    name: "Catppuccin Mocha",
    palette: p(
      "#1e1e2e",
      "#cdd6f4",
      "#cba6f7",
      "#a6e3a1",
      "#6c7086",
      "#fab387",
      "#89b4fa",
      "#f9e2af",
      "#9399b2",
    ),
  },
  {
    id: "solarized",
    name: "Solarized Dark",
    palette: p(
      "#002b36",
      "#839496",
      "#859900",
      "#2aa198",
      "#586e75",
      "#d33682",
      "#268bd2",
      "#b58900",
      "#839496",
    ),
  },
  {
    id: "github-dimmed",
    name: "GitHub Dark Dimmed",
    palette: p(
      "#22272e",
      "#adbac7",
      "#f47067",
      "#96d0ff",
      "#768390",
      "#6cb6ff",
      "#dcbdfb",
      "#f69d50",
      "#adbac7",
    ),
  },
  {
    id: "github-hc",
    name: "GitHub Dark High Contrast",
    palette: p(
      "#0a0c10",
      "#f0f3f6",
      "#ff9492",
      "#addcff",
      "#bdc4cc",
      "#91cbff",
      "#dbb7ff",
      "#ffb757",
      "#f0f3f6",
    ),
  },
  {
    id: "vscode",
    name: "VS Code Dark",
    palette: p(
      "#1e1e1e",
      "#d4d4d4",
      "#569cd6",
      "#ce9178",
      "#6a9955",
      "#b5cea8",
      "#dcdcaa",
      "#4ec9b0",
      "#d4d4d4",
    ),
  },
  {
    id: "xcode",
    name: "Xcode Dark",
    palette: p(
      "#1f1f24",
      "#ffffff",
      "#fc5fa3",
      "#fc6a5d",
      "#6c7986",
      "#d0bf69",
      "#67b7a4",
      "#5dd8ff",
      "#ffffff",
    ),
  },
  {
    id: "dracula-soft",
    name: "Dracula Soft",
    palette: p(
      "#282a36",
      "#f6f6f4",
      "#f286c4",
      "#e7ee98",
      "#7b7f8b",
      "#bf9eee",
      "#62e884",
      "#97e1f1",
      "#f6f6f4",
    ),
  },
  {
    id: "vitesse",
    name: "Vitesse Dark",
    palette: p(
      "#121212",
      "#dbd7ca",
      "#4d9375",
      "#c98a7d",
      "#758575",
      "#4c9a91",
      "#80a665",
      "#5da994",
      "#666666",
    ),
  },
  {
    id: "rose-pine",
    name: "Rosé Pine",
    palette: p(
      "#191724",
      "#e0def4",
      "#31748f",
      "#f6c177",
      "#6e6a86",
      "#c4a7e7",
      "#ebbcba",
      "#9ccfd8",
      "#908caa",
    ),
  },
  {
    id: "ayu",
    name: "Ayu Dark",
    palette: p(
      "#0b0e14",
      "#bfbdb6",
      "#ff8f40",
      "#aad94c",
      "#5a6673",
      "#d2a6ff",
      "#ffb454",
      "#59c2ff",
      "#bfbdb6",
    ),
  },
  {
    id: "gruvbox",
    name: "Gruvbox Dark",
    palette: p(
      "#282828",
      "#ebdbb2",
      "#fb4934",
      "#b8bb26",
      "#928374",
      "#d3869b",
      "#8ec07c",
      "#fabd2f",
      "#ebdbb2",
    ),
  },
  {
    id: "everforest",
    name: "Everforest Dark",
    palette: p(
      "#2d353b",
      "#d3c6aa",
      "#e67e80",
      "#a7c080",
      "#859289",
      "#d699b6",
      "#83c092",
      "#dbbc7f",
      "#d3c6aa",
    ),
  },
  {
    id: "material",
    name: "Material",
    palette: p(
      "#263238",
      "#eeffff",
      "#c792ea",
      "#c3e88d",
      "#546e7a",
      "#f78c6c",
      "#82aaff",
      "#ffcb6b",
      "#89ddff",
    ),
  },
  {
    id: "matrix",
    name: "Matrix",
    palette: p(
      "#000000",
      "#00d93a",
      "#39ff14",
      "#7dff8a",
      "#008f11",
      "#b4ff9f",
      "#00ff41",
      "#5cff6e",
      "#00b32c",
    ),
  },
];

/** The base code size, in px, when the user sets no size. */
export const DEFAULT_CODE_SIZE = 12.5;
/** The size stepper's range, in px, as in the Codex app. */
export const CODE_SIZE_MIN = 8;
export const CODE_SIZE_MAX = 24;

export type CodeAppearance = {
  light: string;
  dark: string;
  /** A CSS font-family list; empty means the app's monospace font. */
  font: string;
  /** Px; 0 means the default size. */
  size: number;
  markers: "color" | "signs";
};

const DEFAULTS: CodeAppearance = {
  light: "skynet",
  dark: "skynet",
  font: "",
  size: 0,
  markers: "color",
};
const KEY = "skynet:code-appearance";
const listeners = new Set<() => void>();
let cache: CodeAppearance | null = null;

/** Read the stored settings, falling back to the defaults for missing or bad values. */
function read(): CodeAppearance {
  if (cache) return cache;
  const next = { ...DEFAULTS };
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(KEY) ?? "{}");
    const v = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
    if (typeof v.light === "string") next.light = v.light;
    if (typeof v.dark === "string") next.dark = v.dark;
    if (typeof v.font === "string") next.font = v.font;
    if (typeof v.size === "number" && Number.isFinite(v.size)) {
      next.size = v.size === 0 ? 0 : Math.min(CODE_SIZE_MAX, Math.max(CODE_SIZE_MIN, v.size));
    }
    if (v.markers === "color" || v.markers === "signs") next.markers = v.markers;
  } catch {
    // Bad JSON: keep the defaults.
  }
  cache = next;
  return next;
}

/** Save a change and tell every subscribed view. */
export function setCodeAppearance(patch: Partial<CodeAppearance>) {
  cache = { ...read(), ...patch };
  try {
    localStorage.setItem(KEY, JSON.stringify(cache));
  } catch {
    // Storage is blocked or full: the change applies to this tab only.
  }
  listeners.forEach((l) => l());
}

/** Put every code setting back to its default. */
export function resetCodeAppearance() {
  setCodeAppearance(DEFAULTS);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  // Another tab changed the settings.
  const onStorage = (e: StorageEvent) => {
    if (e.key !== KEY) return;
    cache = null;
    listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** The current code settings; the defaults on the server. */
export function useCodeAppearance(): CodeAppearance {
  return React.useSyncExternalStore(subscribe, read, () => DEFAULTS);
}

/** Find a theme's palette by id, or the Skynet palette for an unknown id. */
export function codePalette(mode: "light" | "dark", id: string): CodePalette {
  const themes = mode === "light" ? LIGHT_THEMES : DARK_THEMES;
  return (
    themes.find((t) => t.id === id)?.palette ?? (mode === "light" ? SKYNET_LIGHT : SKYNET_DARK)
  );
}

/** Drop the characters that could end the CSS declaration the font goes into, or open a comment. */
export function safeFontFamily(font: string): string {
  return font.replace(/[;{}<>\\*]/g, "").trim();
}
