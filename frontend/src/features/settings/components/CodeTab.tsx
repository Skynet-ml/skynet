"use client";

import * as React from "react";

import {
  CODE_SIZE_MAX,
  CODE_SIZE_MIN,
  DARK_THEMES,
  DEFAULT_CODE_SIZE,
  LIGHT_THEMES,
  codePalette,
  resetCodeAppearance,
  safeFontFamily,
  setCodeAppearance,
  useCodeAppearance,
  type CodeAppearance,
  type CodePalette,
} from "@/shared/lib/code-appearance";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { GitDiff, Moon, Sun, TextT } from "@/shared/ui/icons";
import { NumberInput } from "@/shared/ui/number-input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { Segmented } from "@/shared/ui/segmented";
import { SettingsRow } from "@/shared/ui/settings-row";
import { TOUCH_FIELD_SM } from "@/shared/ui/touch";

const TRIGGER = cn(TOUCH_FIELD_SM, "w-full min-w-0 sm:w-auto sm:min-w-[180px]");

type Mode = "light" | "dark";

// Diff tints for the preview. They follow the preview's own mode, not the app's.
const DIFF = {
  light: { add: "rgba(46,160,67,0.14)", del: "rgba(248,81,73,0.12)" },
  dark: { add: "rgba(46,160,67,0.22)", del: "rgba(248,81,73,0.2)" },
} as const;
const BAR = { add: "#2ea043", del: "#f85149" } as const;

/** One line of the preview diff: the number, the change bar or sign, and the code. */
function PreviewLine({
  mode,
  no,
  kind,
  signs,
  palette,
  children,
}: {
  mode: Mode;
  no: number;
  kind?: "add" | "del";
  signs: boolean;
  palette: CodePalette;
  children: React.ReactNode;
}) {
  const tint = kind && !signs;
  return (
    <div className="flex" style={{ background: tint ? DIFF[mode][kind] : undefined }}>
      <span
        className="w-8 shrink-0 select-none border-s-[3px] pe-2 text-end tabular-nums"
        style={{
          borderColor: tint ? BAR[kind] : "transparent",
          color: palette.comment,
        }}
      >
        {no}
      </span>
      {signs && (
        <span className="w-4 shrink-0 select-none text-center" style={{ color: kind && BAR[kind] }}>
          {kind === "add" ? "+" : kind === "del" ? "\u2212" : ""}
        </span>
      )}
      <span className={cn("whitespace-pre pe-3", signs ? "ps-1" : "ps-3")}>{children}</span>
    </div>
  );
}

/** A small diff in one theme, so the user sees the colors and markers before leaving. */
function Preview({
  mode,
  palette,
  font,
  size,
  signs,
}: {
  mode: Mode;
  palette: CodePalette;
  font: string;
  size: number;
  signs: boolean;
}) {
  const c = palette;
  const line = { mode, signs, palette };
  return (
    <div
      dir="ltr"
      aria-hidden
      className="overflow-x-auto rounded-lg border border-border py-2 leading-[1.6]"
      style={{
        background: c.bg,
        color: c.text,
        fontFamily: font ? `${font}, var(--font-mono)` : "var(--font-mono)",
        fontSize: size,
      }}
    >
      <PreviewLine {...line} no={1}>
        <span style={{ color: c.keyword }}>{"function "}</span>
        <span style={{ color: c.fn }}>{"greet"}</span>
        <span style={{ color: c.punct }}>(</span>
        {"name"}
        <span style={{ color: c.punct }}>: </span>
        <span style={{ color: c.type }}>{"string"}</span>
        <span style={{ color: c.punct }}>{") {"}</span>
      </PreviewLine>
      <PreviewLine {...line} no={2} kind="del">
        <span style={{ color: c.keyword }}>{"  return "}</span>
        <span style={{ color: c.string }}>{'"Hello, "'}</span>
        <span style={{ color: c.punct }}>{" + "}</span>
        {"name"}
        <span style={{ color: c.punct }}>;</span>
      </PreviewLine>
      <PreviewLine {...line} no={2} kind="add">
        <span style={{ color: c.keyword }}>{"  return "}</span>
        <span style={{ color: c.string }}>{"`Hello, ${name}!`"}</span>
        <span style={{ color: c.punct }}>;</span>
      </PreviewLine>
      <PreviewLine {...line} no={3}>
        <span style={{ color: c.punct }}>{"}"}</span>
      </PreviewLine>
    </div>
  );
}

/** The theme select over its preview, as the Claude app shows it. */
function ThemeColumn({
  mode,
  prefs,
  font,
  size,
}: {
  mode: Mode;
  prefs: CodeAppearance;
  font: string;
  size: number;
}) {
  const themes = mode === "light" ? LIGHT_THEMES : DARK_THEMES;
  const Icon = mode === "light" ? Sun : Moon;
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Select value={prefs[mode]} onValueChange={(v) => setCodeAppearance({ [mode]: v })}>
        <SelectTrigger
          size="sm"
          aria-label={msg(
            mode === "light" ? "settings.code.light_theme" : "settings.code.dark_theme",
          )}
          className={cn(TOUCH_FIELD_SM, "w-full min-w-0")}
        >
          <span className="flex min-w-0 items-center gap-2">
            <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
            <SelectValue />
          </span>
        </SelectTrigger>
        <SelectContent>
          {themes.map((t) => (
            <SelectItem key={t.id} value={t.id}>
              {t.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Preview
        mode={mode}
        palette={codePalette(mode, prefs[mode])}
        font={font}
        size={size}
        signs={prefs.markers === "signs"}
      />
    </div>
  );
}

// The 20 most common coding fonts. The menu lists only the ones this device
// has, so a pick always changes the preview.
const MONO_FONTS = [
  "JetBrains Mono",
  "Fira Code",
  "Cascadia Code",
  "SF Mono",
  "Menlo",
  "Monaco",
  "Consolas",
  "Source Code Pro",
  "IBM Plex Mono",
  "Roboto Mono",
  "Ubuntu Mono",
  "Hack",
  "Inconsolata",
  "Iosevka",
  "Victor Mono",
  "Fira Mono",
  "DejaVu Sans Mono",
  "Space Mono",
  "Anonymous Pro",
  "Courier New",
];
// Brand font names are not translated.
const APP_MONO_FONT = "Geist Mono";
const DEFAULT_FONT = "default";

/** Quote a family name for CSS, so names with spaces or digits work. */
function quoteFont(name: string): string {
  return `"${name}"`;
}

/** Tell whether this device draws text in a font: its width differs from a fallback's. */
function fontInstalled(name: string): boolean {
  const ctx = document.createElement("canvas").getContext("2d");
  if (!ctx) return true;
  const sample = "mmmmmmmmmmlli1WQ@#0O";
  return ["monospace", "serif", "sans-serif"].some((fallback) => {
    ctx.font = `72px ${fallback}`;
    const width = ctx.measureText(sample).width;
    ctx.font = `72px ${quoteFont(name)}, ${fallback}`;
    return ctx.measureText(sample).width !== width;
  });
}

/** The code font: the app's font or a common coding font installed on this device. */
function FontField({ value }: { value: string }) {
  const [installed, setInstalled] = React.useState<string[]>([]);
  // Measuring needs a canvas, so it runs after mount.
  React.useEffect(() => setInstalled(MONO_FONTS.filter(fontInstalled)), []);
  // A font saved before this list existed still shows, so the menu never looks empty.
  const listed = value === "" || installed.some((f) => quoteFont(f) === value);

  return (
    <Select
      value={value || DEFAULT_FONT}
      onValueChange={(v) => setCodeAppearance({ font: v === DEFAULT_FONT ? "" : v })}
    >
      <SelectTrigger size="sm" aria-label={msg("settings.code.font")} className={TRIGGER}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={DEFAULT_FONT}>
          <span className="flex items-baseline gap-2">
            <span style={{ fontFamily: "var(--font-mono)" }}>{APP_MONO_FONT}</span>
            <span className="text-xs text-muted-foreground">
              {msg("settings.code.font.default")}
            </span>
          </span>
        </SelectItem>
        {installed.map((f) => (
          <SelectItem key={f} value={quoteFont(f)}>
            <span style={{ fontFamily: quoteFont(f) }}>{f}</span>
          </SelectItem>
        ))}
        {!listed && (
          <SelectItem value={value}>
            <span style={{ fontFamily: safeFontFamily(value) }}>{value.replace(/^"|"$/g, "")}</span>
          </SelectItem>
        )}
      </SelectContent>
    </Select>
  );
}

/** Settings > Code: the code theme, font, size and diff markers, as in the Claude and Codex apps. */
export function CodeTab() {
  const prefs = useCodeAppearance();
  const size = prefs.size || DEFAULT_CODE_SIZE;
  const shownFont = safeFontFamily(prefs.font);

  return (
    <div className="flex flex-col">
      <div className="grid gap-3 border-b border-border/40 py-3 sm:grid-cols-2">
        <ThemeColumn mode="light" prefs={prefs} font={shownFont} size={size} />
        <ThemeColumn mode="dark" prefs={prefs} font={shownFont} size={size} />
      </div>
      <SettingsRow
        icon={TextT}
        label={msg("settings.code.font")}
        description={msg("settings.code.font.description")}
      >
        <FontField value={prefs.font} />
      </SettingsRow>
      <SettingsRow
        icon={TextT}
        label={msg("settings.code.size")}
        description={msg("settings.code.size.description")}
      >
        <span className="flex items-center gap-2">
          <NumberInput
            size="sm"
            min={CODE_SIZE_MIN}
            max={CODE_SIZE_MAX}
            step={0.5}
            decimals={1}
            value={size}
            aria-label={msg("settings.code.size")}
            onChange={(next) => setCodeAppearance({ size: next })}
            className="w-28"
          />
          <span className="text-xs text-muted-foreground">{msg("settings.code.size.px")}</span>
        </span>
      </SettingsRow>
      <SettingsRow
        icon={GitDiff}
        label={msg("settings.code.markers")}
        description={msg("settings.code.markers.description")}
      >
        <Segmented<CodeAppearance["markers"]>
          size="sm"
          value={prefs.markers}
          onChange={(markers) => setCodeAppearance({ markers })}
          label={msg("settings.code.markers")}
          options={[
            { value: "color", label: msg("settings.code.markers.color") },
            { value: "signs", label: msg("settings.code.markers.signs") },
          ]}
        />
      </SettingsRow>
      <button
        type="button"
        onClick={resetCodeAppearance}
        className="mt-3 self-end rounded text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {msg("settings.code.reset")}
      </button>
    </div>
  );
}
