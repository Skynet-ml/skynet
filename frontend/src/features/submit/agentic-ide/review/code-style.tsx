// Applies Settings > Code to the review pane's diff, file and editor views.
// The rules are unlayered, so they beat the Tailwind size utilities on those views.

import {
  codePalette,
  safeFontFamily,
  useCodeAppearance,
  type CodePalette,
} from "@/shared/lib/code-appearance";

const vars = (c: CodePalette) =>
  `--ide-code-bg:${c.bg};--ide-popup-bg:${c.bg};--code-text:${c.text};--code-keyword:${c.keyword};--code-accent:${c.keyword};` +
  `--code-string:${c.string};--code-comment:${c.comment};--code-number:${c.number};--code-function:${c.fn};` +
  `--code-type:${c.type};--code-punct:${c.punct};--code-operator:${c.punct};`;

export function CodeAppearanceStyle() {
  const { light, dark, font: stored, size } = useCodeAppearance();
  const font = safeFontFamily(stored);
  const rules: string[] = [];
  // The Skynet themes are the app's own colors, so they need no rule.
  if (light !== "skynet")
    rules.push(`html:not(.dark) :is(.ide-code,.ide-editor){${vars(codePalette("light", light))}}`);
  if (dark !== "skynet")
    rules.push(`html.dark :is(.ide-code,.ide-editor){${vars(codePalette("dark", dark))}}`);
  if (light !== "skynet" || dark !== "skynet")
    rules.push(`html .ide-editor .cm-editor{color:var(--code-text)}`);
  const type = [
    font && `font-family:${font},var(--font-mono)`,
    size > 0 && `font-size:${size}px`,
  ].filter(Boolean);
  if (type.length)
    rules.push(
      `html :is(.ide-code,.ide-editor .cm-editor,.ide-editor .cm-scroller,.ide-editor .cm-tooltip.cm-tooltip-autocomplete){${type.join(";")}}`,
    );
  return rules.length ? <style>{rules.join("\n")}</style> : null;
}
