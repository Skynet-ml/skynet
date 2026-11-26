"use client";

import * as React from "react";
import { DirectionProvider } from "@radix-ui/react-direction";
import { MotionConfig } from "motion/react";
import { dirForLocale } from "@/shared/lib/locale";
import { useLocale } from "@/shared/providers/locale-provider";
import {
  COLOR_THEME_STORAGE_KEY,
  applyResolvedColorTheme,
  readColorTheme,
  resolveColorTheme,
  watchSystemColorScheme,
  writeColorTheme,
  type ColorTheme,
  type ResolvedColorTheme,
} from "@/shared/lib/color-theme";

interface ColorThemeContextValue {
  /** The stored choice: System, Light, Gray or Dark. */
  theme: ColorTheme;
  /** What is painted right now. */
  resolved: ResolvedColorTheme;
  setTheme: (theme: ColorTheme) => void;
}

const ColorThemeContext = React.createContext<ColorThemeContextValue | null>(null);

function useColorThemeState(): ColorThemeContextValue {
  // The server can't see the stored choice, so the first render assumes the
  // default; the head script already painted the right theme, and the effect
  // below syncs this state to it right after hydration.
  const [theme, setThemeState] = React.useState<ColorTheme>("system");
  const [resolved, setResolved] = React.useState<ResolvedColorTheme>("light");
  const [loaded, setLoaded] = React.useState(false);

  React.useEffect(() => {
    setThemeState(readColorTheme());
    setLoaded(true);
  }, []);

  React.useEffect(() => {
    // Until the stored choice is read, the head script's paint stands; applying
    // the "system" placeholder here would flash the wrong theme for a frame.
    if (!loaded) return;
    const apply = () => {
      const next = resolveColorTheme(theme);
      applyResolvedColorTheme(next);
      setResolved(next);
    };
    apply();
    if (theme !== "system") return;
    return watchSystemColorScheme(apply);
  }, [theme, loaded]);

  // Another tab changed the choice: follow it.
  React.useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === COLOR_THEME_STORAGE_KEY || e.key === null) setThemeState(readColorTheme());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const setTheme = React.useCallback((next: ColorTheme) => {
    writeColorTheme(next);
    setThemeState(next);
  }, []);

  return React.useMemo(() => ({ theme, resolved, setTheme }), [theme, resolved, setTheme]);
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const { locale } = useLocale();
  const colorTheme = useColorThemeState();
  return (
    <ColorThemeContext.Provider value={colorTheme}>
      <DirectionProvider dir={dirForLocale(locale)}>
        {/* Motion defaults to "never"; "user" honours the OS reduced-motion setting. */}
        <MotionConfig reducedMotion="user">{children}</MotionConfig>
      </DirectionProvider>
    </ColorThemeContext.Provider>
  );
}

/** The color theme choice and its setter; must sit under ThemeProvider. */
export function useColorTheme(): ColorThemeContextValue {
  const ctx = React.useContext(ColorThemeContext);
  if (!ctx) throw new Error("useColorTheme must be used within ThemeProvider");
  return ctx;
}
